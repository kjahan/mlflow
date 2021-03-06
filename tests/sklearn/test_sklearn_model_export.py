from __future__ import print_function

import sys
import os
import pickle
import pytest
import yaml
import json
from collections import namedtuple

import numpy as np
import pandas as pd
import pandas.testing
import sklearn
import sklearn.datasets as datasets
import sklearn.linear_model as glm
import sklearn.neighbors as knn
from sklearn.pipeline import Pipeline as SKPipeline
from sklearn.preprocessing import FunctionTransformer as SKFunctionTransformer

import mlflow.sklearn
import mlflow.utils
import mlflow.pyfunc.scoring_server as pyfunc_scoring_server
from mlflow import pyfunc
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import INVALID_PARAMETER_VALUE
from mlflow.models import Model
from mlflow.tracking.utils import _get_model_log_dir
from mlflow.utils.environment import _mlflow_conda_env
from mlflow.utils.file_utils import TempDir
from mlflow.utils.model_utils import _get_flavor_configuration

from tests.helper_functions import score_model_in_sagemaker_docker_container

ModelWithData = namedtuple("ModelWithData", ["model", "inference_data"])


@pytest.fixture(scope="session")
def sklearn_knn_model():
    iris = datasets.load_iris()
    X = iris.data[:, :2]  # we only take the first two features.
    y = iris.target
    knn_model = knn.KNeighborsClassifier()
    knn_model.fit(X, y)
    return ModelWithData(model=knn_model, inference_data=X)


@pytest.fixture(scope="session")
def sklearn_logreg_model():
    iris = datasets.load_iris()
    X = iris.data[:, :2]  # we only take the first two features.
    y = iris.target
    linear_lr = glm.LogisticRegression()
    linear_lr.fit(X, y)
    return ModelWithData(model=linear_lr, inference_data=X)


@pytest.fixture(scope="session")
def sklearn_custom_transformer_model(sklearn_knn_model):
    def transform(vec):
        print("Invoking custom transformer!")
        return vec + 1

    transformer = SKFunctionTransformer(transform, validate=True)
    pipeline = SKPipeline([("custom_transformer", transformer), ("knn", sklearn_knn_model.model)])
    return ModelWithData(pipeline, inference_data=datasets.load_iris().data[:, :2])


@pytest.fixture
def model_path(tmpdir):
    return os.path.join(str(tmpdir), "model")


@pytest.fixture
def sklearn_custom_env(tmpdir):
    conda_env = os.path.join(str(tmpdir), "conda_env.yml")
    _mlflow_conda_env(
            conda_env,
            additional_conda_deps=["scikit-learn", "pytest"])
    return conda_env


def test_model_save_load(sklearn_knn_model, model_path):
    knn_model = sklearn_knn_model.model

    mlflow.sklearn.save_model(sk_model=knn_model, path=model_path)
    reloaded_knn_model = mlflow.sklearn.load_model(path=model_path)
    reloaded_knn_pyfunc = pyfunc.load_pyfunc(path=model_path)

    np.testing.assert_array_equal(
            knn_model.predict(sklearn_knn_model.inference_data),
            reloaded_knn_model.predict(sklearn_knn_model.inference_data))

    np.testing.assert_array_equal(
            reloaded_knn_model.predict(sklearn_knn_model.inference_data),
            reloaded_knn_pyfunc.predict(sklearn_knn_model.inference_data))


def test_model_log(sklearn_logreg_model, model_path):
    old_uri = mlflow.get_tracking_uri()
    with TempDir(chdr=True, remove_on_exit=True) as tmp:
        for should_start_run in [False, True]:
            try:
                mlflow.set_tracking_uri("test")
                if should_start_run:
                    mlflow.start_run()

                artifact_path = "linear"
                conda_env = os.path.join(tmp.path(), "conda_env.yaml")
                _mlflow_conda_env(conda_env, additional_pip_deps=["scikit-learn"])

                mlflow.sklearn.log_model(
                        sk_model=sklearn_logreg_model.model,
                        artifact_path=artifact_path,
                        conda_env=conda_env)
                run_id = mlflow.active_run().info.run_uuid

                reloaded_logreg_model = mlflow.sklearn.load_model(artifact_path, run_id)
                np.testing.assert_array_equal(
                        sklearn_logreg_model.model.predict(sklearn_logreg_model.inference_data),
                        reloaded_logreg_model.predict(sklearn_logreg_model.inference_data))

                model_path = _get_model_log_dir(
                        artifact_path,
                        run_id=run_id)
                model_config = Model.load(os.path.join(model_path, "MLmodel"))
                assert pyfunc.FLAVOR_NAME in model_config.flavors
                assert pyfunc.ENV in model_config.flavors[pyfunc.FLAVOR_NAME]
                env_path = model_config.flavors[pyfunc.FLAVOR_NAME][pyfunc.ENV]
                assert os.path.exists(os.path.join(model_path, env_path))

            finally:
                mlflow.end_run()
                mlflow.set_tracking_uri(old_uri)


def test_custom_transformer_can_be_saved_and_loaded_with_cloudpickle_format(
        sklearn_custom_transformer_model, tmpdir):
    custom_transformer_model = sklearn_custom_transformer_model.model

    # Because the model contains a customer transformer that is not defined at the top level of the
    # current test module, we expect pickle to fail when attempting to serialize it. In contrast,
    # we expect cloudpickle to successfully locate the transformer definition and serialize the
    # model successfully.
    if sys.version_info >= (3, 0):
        expect_exception_context = pytest.raises(AttributeError)
    else:
        expect_exception_context = pytest.raises(pickle.PicklingError)
    with expect_exception_context:
        pickle_format_model_path = os.path.join(str(tmpdir), "pickle_model")
        mlflow.sklearn.save_model(sk_model=custom_transformer_model,
                                  path=pickle_format_model_path,
                                  serialization_format=mlflow.sklearn.SERIALIZATION_FORMAT_PICKLE)

    cloudpickle_format_model_path = os.path.join(str(tmpdir), "cloud_pickle_model")
    mlflow.sklearn.save_model(sk_model=custom_transformer_model,
                              path=cloudpickle_format_model_path,
                              serialization_format=mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE)

    reloaded_custom_transformer_model = mlflow.sklearn.load_model(
            path=cloudpickle_format_model_path)

    np.testing.assert_array_equal(
            custom_transformer_model.predict(sklearn_custom_transformer_model.inference_data),
            reloaded_custom_transformer_model.predict(
                sklearn_custom_transformer_model.inference_data))


def test_model_save_persists_specified_conda_env_in_mlflow_model_directory(
        sklearn_knn_model, model_path, sklearn_custom_env):
    mlflow.sklearn.save_model(
            sk_model=sklearn_knn_model.model, path=model_path, conda_env=sklearn_custom_env)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)
    assert saved_conda_env_path != sklearn_custom_env

    with open(sklearn_custom_env, "r") as f:
        sklearn_custom_env_parsed = yaml.safe_load(f)
    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == sklearn_custom_env_parsed


def test_model_save_accepts_conda_env_as_dict(sklearn_knn_model, model_path):
    conda_env = dict(mlflow.sklearn.DEFAULT_CONDA_ENV)
    conda_env["dependencies"].append("pytest")
    mlflow.sklearn.save_model(
            sk_model=sklearn_knn_model.model, path=model_path, conda_env=conda_env)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)

    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == conda_env


def test_model_log_persists_specified_conda_env_in_mlflow_model_directory(
        sklearn_knn_model, sklearn_custom_env):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.sklearn.log_model(sk_model=sklearn_knn_model.model,
                                 artifact_path=artifact_path,
                                 conda_env=sklearn_custom_env)
        run_id = mlflow.active_run().info.run_uuid
    model_path = _get_model_log_dir(artifact_path, run_id)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)
    assert saved_conda_env_path != sklearn_custom_env

    with open(sklearn_custom_env, "r") as f:
        sklearn_custom_env_parsed = yaml.safe_load(f)
    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == sklearn_custom_env_parsed


def test_model_save_throws_exception_if_serialization_format_is_unrecognized(
        sklearn_knn_model, model_path):
    with pytest.raises(MlflowException) as exc:
        mlflow.sklearn.save_model(sk_model=sklearn_knn_model.model, path=model_path,
                                  serialization_format="not a valid format")
        assert exc.error_code == INVALID_PARAMETER_VALUE

    # The unsupported serialization format should have been detected prior to the execution of
    # any directory creation or state-mutating persistence logic that would prevent a second
    # serialization call with the same model path from succeeding
    assert not os.path.exists(model_path)
    mlflow.sklearn.save_model(sk_model=sklearn_knn_model.model, path=model_path)


def test_model_save_without_specified_conda_env_uses_default_env_with_expected_dependencies(
        sklearn_knn_model, model_path):
    knn_model = sklearn_knn_model.model
    mlflow.sklearn.save_model(sk_model=knn_model, path=model_path, conda_env=None)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    with open(conda_env_path, "r") as f:
        conda_env = yaml.safe_load(f)

    assert conda_env == mlflow.sklearn.DEFAULT_CONDA_ENV


def test_model_log_without_specified_conda_env_uses_default_env_with_expected_dependencies(
        sklearn_knn_model):
    artifact_path = "model"
    knn_model = sklearn_knn_model.model
    with mlflow.start_run():
        mlflow.sklearn.log_model(sk_model=knn_model, artifact_path=artifact_path, conda_env=None)
        run_id = mlflow.active_run().info.run_uuid
    model_path = _get_model_log_dir(artifact_path, run_id)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    with open(conda_env_path, "r") as f:
        conda_env = yaml.safe_load(f)

    assert conda_env == mlflow.sklearn.DEFAULT_CONDA_ENV


def test_model_save_uses_cloudpickle_serialization_format_by_default(sklearn_knn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_knn_model.model, path=model_path, conda_env=None)

    sklearn_conf = _get_flavor_configuration(
            model_path=model_path, flavor_name=mlflow.sklearn.FLAVOR_NAME)
    assert "serialization_format" in sklearn_conf
    assert sklearn_conf["serialization_format"] == mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE


def test_model_log_uses_cloudpickle_serialization_format_by_default(sklearn_knn_model):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.sklearn.log_model(
                sk_model=sklearn_knn_model.model, artifact_path=artifact_path, conda_env=None)
        run_id = mlflow.active_run().info.run_uuid
    model_path = _get_model_log_dir(artifact_path, run_id)

    sklearn_conf = _get_flavor_configuration(
            model_path=model_path, flavor_name=mlflow.sklearn.FLAVOR_NAME)
    assert "serialization_format" in sklearn_conf
    assert sklearn_conf["serialization_format"] == mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE


@pytest.mark.release
def test_sagemaker_docker_model_scoring_with_default_conda_env(sklearn_knn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_knn_model.model, path=model_path, conda_env=None)
    reloaded_knn_pyfunc = pyfunc.load_pyfunc(path=model_path)

    inference_df = pd.DataFrame(sklearn_knn_model.inference_data)
    scoring_response = score_model_in_sagemaker_docker_container(
            model_path=model_path,
            data=inference_df,
            content_type=pyfunc_scoring_server.CONTENT_TYPE_JSON_SPLIT_ORIENTED,
            flavor=mlflow.pyfunc.FLAVOR_NAME)
    deployed_model_preds = pd.DataFrame(json.loads(scoring_response.content))

    pandas.testing.assert_frame_equal(
        deployed_model_preds,
        pd.DataFrame(reloaded_knn_pyfunc.predict(inference_df)),
        check_dtype=False,
        check_less_precise=6)
