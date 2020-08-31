"""
The ``mlflow.sklearn`` module provides an API for logging and loading scikit-learn models. This
module exports scikit-learn models with the following flavors:

Python (native) `pickle <https://scikit-learn.org/stable/modules/model_persistence.html>`_ format
    This is the main flavor that can be loaded back into scikit-learn.

:py:mod:`mlflow.pyfunc`
    Produced for use by generic pyfunc-based deployment tools and batch inference.
"""
import functools
import gorilla
import os
import gorilla
import logging
import pickle
import yaml
import warnings


import mlflow
from mlflow import pyfunc
from mlflow.entities.run_status import RunStatus
from mlflow.exceptions import MlflowException
from mlflow.models import Model
from mlflow.models.model import MLMODEL_FILE_NAME
from mlflow.models.signature import ModelSignature
from mlflow.models.utils import ModelInputExample, _save_example
from mlflow.protos.databricks_pb2 import INVALID_PARAMETER_VALUE, INTERNAL_ERROR
from mlflow.protos.databricks_pb2 import RESOURCE_ALREADY_EXISTS
from mlflow.tracking.artifact_utils import _download_artifact_from_uri
from mlflow.utils.annotations import experimental
from mlflow.utils.environment import _mlflow_conda_env
from mlflow.utils.model_utils import _get_flavor_configuration
from mlflow.utils.autologging_utils import try_mlflow_log

FLAVOR_NAME = "sklearn"

SERIALIZATION_FORMAT_PICKLE = "pickle"
SERIALIZATION_FORMAT_CLOUDPICKLE = "cloudpickle"

SUPPORTED_SERIALIZATION_FORMATS = [SERIALIZATION_FORMAT_PICKLE, SERIALIZATION_FORMAT_CLOUDPICKLE]

_logger = logging.getLogger(__name__)


def get_default_conda_env(include_cloudpickle=False):
    """
    :return: The default Conda environment for MLflow Models produced by calls to
             :func:`save_model()` and :func:`log_model()`.
    """
    import sklearn

    pip_deps = None
    if include_cloudpickle:
        import cloudpickle

        pip_deps = ["cloudpickle=={}".format(cloudpickle.__version__)]
    return _mlflow_conda_env(
        additional_conda_deps=["scikit-learn={}".format(sklearn.__version__)],
        additional_pip_deps=pip_deps,
        additional_conda_channels=None,
    )


def save_model(
    sk_model,
    path,
    conda_env=None,
    mlflow_model=None,
    serialization_format=SERIALIZATION_FORMAT_CLOUDPICKLE,
    signature: ModelSignature = None,
    input_example: ModelInputExample = None,
):
    """
    Save a scikit-learn model to a path on the local file system.

    :param sk_model: scikit-learn model to be saved.
    :param path: Local path where the model is to be saved.
    :param conda_env: Either a dictionary representation of a Conda environment or the path to a
                      Conda environment yaml file. If provided, this decsribes the environment
                      this model should be run in. At minimum, it should specify the dependencies
                      contained in :func:`get_default_conda_env()`. If `None`, the default
                      :func:`get_default_conda_env()` environment is added to the model.
                      The following is an *example* dictionary representation of a Conda
                      environment::

                        {
                            'name': 'mlflow-env',
                            'channels': ['defaults'],
                            'dependencies': [
                                'python=3.7.0',
                                'scikit-learn=0.19.2'
                            ]
                        }

    :param mlflow_model: :py:mod:`mlflow.models.Model` this flavor is being added to.
    :param serialization_format: The format in which to serialize the model. This should be one of
                                 the formats listed in
                                 ``mlflow.sklearn.SUPPORTED_SERIALIZATION_FORMATS``. The Cloudpickle
                                 format, ``mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE``,
                                 provides better cross-system compatibility by identifying and
                                 packaging code dependencies with the serialized model.

    :param signature: (Experimental) :py:class:`ModelSignature <mlflow.models.ModelSignature>`
                      describes model input and output :py:class:`Schema <mlflow.types.Schema>`.
                      The model signature can be :py:func:`inferred <mlflow.models.infer_signature>`
                      from datasets with valid model input (e.g. the training dataset with target
                      column omitted) and valid model output (e.g. model predictions generated on
                      the training dataset), for example:

                      .. code-block:: python

                        from mlflow.models.signature import infer_signature
                        train = df.drop_column("target_label")
                        predictions = ... # compute model predictions
                        signature = infer_signature(train, predictions)
    :param input_example: (Experimental) Input example provides one or several instances of valid
                          model input. The example can be used as a hint of what data to feed the
                          model. The given example will be converted to a Pandas DataFrame and then
                          serialized to json using the Pandas split-oriented format. Bytes are
                          base64-encoded.


    .. code-block:: python
        :caption: Example

        import mlflow.sklearn
        from sklearn.datasets import load_iris
        from sklearn import tree

        iris = load_iris()
        sk_model = tree.DecisionTreeClassifier()
        sk_model = sk_model.fit(iris.data, iris.target)

        # Save the model in cloudpickle format
        # set path to location for persistence
        sk_path_dir_1 = ...
        mlflow.sklearn.save_model(
                sk_model, sk_path_dir_1,
                serialization_format=mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE)

        # save the model in pickle format
        # set path to location for persistence
        sk_path_dir_2 = ...
        mlflow.sklearn.save_model(sk_model, sk_path_dir_2,
                                  serialization_format=mlflow.sklearn.SERIALIZATION_FORMAT_PICKLE)
    """
    import sklearn

    if serialization_format not in SUPPORTED_SERIALIZATION_FORMATS:
        raise MlflowException(
            message=(
                "Unrecognized serialization format: {serialization_format}. Please specify one"
                " of the following supported formats: {supported_formats}.".format(
                    serialization_format=serialization_format,
                    supported_formats=SUPPORTED_SERIALIZATION_FORMATS,
                )
            ),
            error_code=INVALID_PARAMETER_VALUE,
        )

    if os.path.exists(path):
        raise MlflowException(
            message="Path '{}' already exists".format(path), error_code=RESOURCE_ALREADY_EXISTS
        )
    os.makedirs(path)
    if mlflow_model is None:
        mlflow_model = Model()
    if signature is not None:
        mlflow_model.signature = signature
    if input_example is not None:
        _save_example(mlflow_model, input_example, path)

    model_data_subpath = "model.pkl"
    _save_model(
        sk_model=sk_model,
        output_path=os.path.join(path, model_data_subpath),
        serialization_format=serialization_format,
    )

    conda_env_subpath = "conda.yaml"
    if conda_env is None:
        conda_env = get_default_conda_env(
            include_cloudpickle=serialization_format == SERIALIZATION_FORMAT_CLOUDPICKLE
        )
    elif not isinstance(conda_env, dict):
        with open(conda_env, "r") as f:
            conda_env = yaml.safe_load(f)
    with open(os.path.join(path, conda_env_subpath), "w") as f:
        yaml.safe_dump(conda_env, stream=f, default_flow_style=False)

    # `PyFuncModel` only works for sklearn models that define `predict()`.
    if hasattr(sk_model, "predict"):
        pyfunc.add_to_model(
            mlflow_model,
            loader_module="mlflow.sklearn",
            model_path=model_data_subpath,
            env=conda_env_subpath,
        )
    mlflow_model.add_flavor(
        FLAVOR_NAME,
        pickled_model=model_data_subpath,
        sklearn_version=sklearn.__version__,
        serialization_format=serialization_format,
    )
    mlflow_model.save(os.path.join(path, MLMODEL_FILE_NAME))


def log_model(
    sk_model,
    artifact_path,
    conda_env=None,
    serialization_format=SERIALIZATION_FORMAT_CLOUDPICKLE,
    registered_model_name=None,
    signature: ModelSignature = None,
    input_example: ModelInputExample = None,
):
    """
    Log a scikit-learn model as an MLflow artifact for the current run.

    :param sk_model: scikit-learn model to be saved.
    :param artifact_path: Run-relative artifact path.
    :param conda_env: Either a dictionary representation of a Conda environment or the path to a
                      Conda environment yaml file. If provided, this decsribes the environment
                      this model should be run in. At minimum, it should specify the dependencies
                      contained in :func:`get_default_conda_env()`. If `None`, the default
                      :func:`get_default_conda_env()` environment is added to the model.
                      The following is an *example* dictionary representation of a Conda
                      environment::

                        {
                            'name': 'mlflow-env',
                            'channels': ['defaults'],
                            'dependencies': [
                                'python=3.7.0',
                                'scikit-learn=0.19.2'
                            ]
                        }

    :param serialization_format: The format in which to serialize the model. This should be one of
                                 the formats listed in
                                 ``mlflow.sklearn.SUPPORTED_SERIALIZATION_FORMATS``. The Cloudpickle
                                 format, ``mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE``,
                                 provides better cross-system compatibility by identifying and
                                 packaging code dependencies with the serialized model.
    :param registered_model_name: (Experimental) If given, create a model version under
                                  ``registered_model_name``, also creating a registered model if one
                                  with the given name does not exist.

    :param signature: (Experimental) :py:class:`ModelSignature <mlflow.models.ModelSignature>`
                      describes model input and output :py:class:`Schema <mlflow.types.Schema>`.
                      The model signature can be :py:func:`inferred <mlflow.models.infer_signature>`
                      from datasets with valid model input (e.g. the training dataset with target
                      column omitted) and valid model output (e.g. model predictions generated on
                      the training dataset), for example:

                      .. code-block:: python

                        from mlflow.models.signature import infer_signature
                        train = df.drop_column("target_label")
                        predictions = ... # compute model predictions
                        signature = infer_signature(train, predictions)
    :param input_example: (Experimental) Input example provides one or several instances of valid
                          model input. The example can be used as a hint of what data to feed the
                          model. The given example will be converted to a Pandas DataFrame and then
                          serialized to json using the Pandas split-oriented format. Bytes are
                          base64-encoded.



    .. code-block:: python
        :caption: Example

        import mlflow
        import mlflow.sklearn
        from sklearn.datasets import load_iris
        from sklearn import tree

        iris = load_iris()
        sk_model = tree.DecisionTreeClassifier()
        sk_model = sk_model.fit(iris.data, iris.target)
        # set the artifact_path to location where experiment artifacts will be saved

        #log model params
        mlflow.log_param("criterion", sk_model.criterion)
        mlflow.log_param("splitter", sk_model.splitter)

        # log model
        mlflow.sklearn.log_model(sk_model, "sk_models")
    """
    return Model.log(
        artifact_path=artifact_path,
        flavor=mlflow.sklearn,
        sk_model=sk_model,
        conda_env=conda_env,
        serialization_format=serialization_format,
        registered_model_name=registered_model_name,
        signature=signature,
        input_example=input_example,
    )


def _load_model_from_local_file(path, serialization_format):
    """Load a scikit-learn model saved as an MLflow artifact on the local file system.

    :param path: Local filesystem path to the MLflow Model saved with the ``sklearn`` flavor
    :param serialization_format: The format in which the model was serialized. This should be one of
                                 the following: ``mlflow.sklearn.SERIALIZATION_FORMAT_PICKLE`` or
                                 ``mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE``.
    """
    # TODO: we could validate the scikit-learn version here
    if serialization_format not in SUPPORTED_SERIALIZATION_FORMATS:
        raise MlflowException(
            message=(
                "Unrecognized serialization format: {serialization_format}. Please specify one"
                " of the following supported formats: {supported_formats}.".format(
                    serialization_format=serialization_format,
                    supported_formats=SUPPORTED_SERIALIZATION_FORMATS,
                )
            ),
            error_code=INVALID_PARAMETER_VALUE,
        )
    with open(path, "rb") as f:
        # Models serialized with Cloudpickle cannot necessarily be deserialized using Pickle;
        # That's why we check the serialization format of the model before deserializing
        if serialization_format == SERIALIZATION_FORMAT_PICKLE:
            return pickle.load(f)
        elif serialization_format == SERIALIZATION_FORMAT_CLOUDPICKLE:
            import cloudpickle

            return cloudpickle.load(f)


def _load_pyfunc(path):
    """
    Load PyFunc implementation. Called by ``pyfunc.load_pyfunc``.

    :param path: Local filesystem path to the MLflow Model with the ``sklearn`` flavor.
    """
    if os.path.isfile(path):
        # Scikit-learn models saved in older versions of MLflow (<= 1.9.1) specify the ``data``
        # field within the pyfunc flavor configuration. For these older models, the ``path``
        # parameter of ``_load_pyfunc()`` refers directly to a serialized scikit-learn model
        # object. In this case, we assume that the serialization format is ``pickle``, since
        # the model loading procedure in older versions of MLflow used ``pickle.load()``.
        serialization_format = SERIALIZATION_FORMAT_PICKLE
    else:
        # In contrast, scikit-learn models saved in versions of MLflow > 1.9.1 do not
        # specify the ``data`` field within the pyfunc flavor configuration. For these newer
        # models, the ``path`` parameter of ``load_pyfunc()`` refers to the top-level MLflow
        # Model directory. In this case, we parse the model path from the MLmodel's pyfunc
        # flavor configuration and attempt to fetch the serialization format from the
        # scikit-learn flavor configuration
        try:
            sklearn_flavor_conf = _get_flavor_configuration(
                model_path=path, flavor_name=FLAVOR_NAME
            )
            serialization_format = sklearn_flavor_conf.get(
                "serialization_format", SERIALIZATION_FORMAT_PICKLE
            )
        except MlflowException:
            _logger.warning(
                "Could not find scikit-learn flavor configuration during model loading process."
                " Assuming 'pickle' serialization format."
            )
            serialization_format = SERIALIZATION_FORMAT_PICKLE

        pyfunc_flavor_conf = _get_flavor_configuration(
            model_path=path, flavor_name=pyfunc.FLAVOR_NAME
        )
        path = os.path.join(path, pyfunc_flavor_conf["model_path"])

    return _load_model_from_local_file(path=path, serialization_format=serialization_format)


def _save_model(sk_model, output_path, serialization_format):
    """
    :param sk_model: The scikit-learn model to serialize.
    :param output_path: The file path to which to write the serialized model.
    :param serialization_format: The format in which to serialize the model. This should be one of
                                 the following: ``mlflow.sklearn.SERIALIZATION_FORMAT_PICKLE`` or
                                 ``mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE``.
    """
    with open(output_path, "wb") as out:
        if serialization_format == SERIALIZATION_FORMAT_PICKLE:
            pickle.dump(sk_model, out)
        elif serialization_format == SERIALIZATION_FORMAT_CLOUDPICKLE:
            import cloudpickle

            cloudpickle.dump(sk_model, out)
        else:
            raise MlflowException(
                message="Unrecognized serialization format: {serialization_format}".format(
                    serialization_format=serialization_format
                ),
                error_code=INTERNAL_ERROR,
            )


def load_model(model_uri):
    """
    Load a scikit-learn model from a local file or a run.

    :param model_uri: The location, in URI format, of the MLflow model, for example:

                      - ``/Users/me/path/to/local/model``
                      - ``relative/path/to/local/model``
                      - ``s3://my_bucket/path/to/model``
                      - ``runs:/<mlflow_run_id>/run-relative/path/to/model``
                      - ``models:/<model_name>/<model_version>``
                      - ``models:/<model_name>/<stage>``

                      For more information about supported URI schemes, see
                      `Referencing Artifacts <https://www.mlflow.org/docs/latest/concepts.html#
                      artifact-locations>`_.

    :return: A scikit-learn model.

    .. code-block:: python
        :caption: Example

        import mlflow.sklearn
        sk_model = mlflow.sklearn.load_model("runs:/96771d893a5e46159d9f3b49bf9013e2/sk_models")

        # use Pandas DataFrame to make predictions
        pandas_df = ...
        predictions = sk_model.predict(pandas_df)
    """
    local_model_path = _download_artifact_from_uri(artifact_uri=model_uri)
    flavor_conf = _get_flavor_configuration(model_path=local_model_path, flavor_name=FLAVOR_NAME)
    sklearn_model_artifacts_path = os.path.join(local_model_path, flavor_conf["pickled_model"])
    serialization_format = flavor_conf.get("serialization_format", SERIALIZATION_FORMAT_PICKLE)
    return _load_model_from_local_file(
        path=sklearn_model_artifacts_path, serialization_format=serialization_format
    )


# NOTE: The current implementation doesn't guarantee thread-safety, but that's okay for now because:
# 1. We don't currently have any use cases for allow_children=True.
# 2. The list append & pop operations are thread-safe, so we will always clear the session stack
#    once all _SklearnTrainingSessions exit.
class _SklearnTrainingSession(object):
    _session_stack = []

    def __init__(self, clazz, allow_children=True):
        """
        A session manager for nested autologging runs.

        :param clazz: A class object that this session originates from.
        :param allow_children: If True, allows autologging in child sessions.
                               If False, disallows autologging in all descendant sessions.

        Example:

        >>> class Parent: pass
        >>> class Child: pass
        >>> class Grandchild: pass

        >>> with _SklearnTrainingSession(Parent, False) as p:
        ...     with _SklearnTrainingSession(Child, True) as c:
        ...         with _SklearnTrainingSession(Grandchild, True) as g:
        ...             print(p.should_log())
        ...             print(c.should_log())
        ...             print(g.should_log())
        True
        False
        False

        >>> with _SklearnTrainingSession(Parent, True) as p:
        ...     with _SklearnTrainingSession(Child, False) as c:
        ...         with _SklearnTrainingSession(Grandchild, True) as g:
        ...             print(p.should_log())
        ...             print(c.should_log())
        ...             print(g.should_log())
        True
        True
        False

        >>> with _SklearnTrainingSession(Child, True) as c1:
        ...     with _SklearnTrainingSession(Child, True) as c2:
        ...             print(c1.should_log())
        ...             print(c2.should_log())
        True
        False
        """
        self.allow_children = allow_children
        self.clazz = clazz
        self._parent = None

    def __enter__(self):
        if len(_SklearnTrainingSession._session_stack) > 0:
            self._parent = _SklearnTrainingSession._session_stack[-1]
            self.allow_children = (
                _SklearnTrainingSession._session_stack[-1].allow_children and self.allow_children
            )
        _SklearnTrainingSession._session_stack.append(self)
        return self

    def __exit__(self, tp, val, traceback):
        _SklearnTrainingSession._session_stack.pop()

    def should_log(self):
        """
        Returns True when at least one of the following conditions satisfies:

        1. This session is the root session.
        2. The parent session allows autologging and its class differs from this session's class.
        """
        return (self._parent is None) or (
            self._parent.allow_children and self._parent.clazz != self.clazz
        )


@experimental
def autolog():
    """
    Enables autologging for scikit-learn estimators.

    **When is autologging performed?**
      Autologging is performed when you call:

      - ``estimator.fit()``
      - ``estimator.fit_predict()``
      - ``estimator.fit_transform()``

    **Logged information**
      **Parameters**
        - Parameters obtained by ``estimator.get_params(deep=True)``. Note that ``get_params``
          is called with ``deep=True``. This means when you fit a meta estimator that chains
          a series of estimators, the parameters of these child estimators are also logged.

      **Metrics**
        - A training score obtained by ``estimator.score``. Note that the training score is
          computed using parameters given to ``fit()``.

      **Tags**
        - An estimator class name (e.g. "LinearRegression").
        - A fully qualified estimator class name
          (e.g. "sklearn.linear_model._base.LinearRegression").

      **Artifacts**
        - A fitted estimator (logged by :py:func:`mlflow.sklearn.log_model()`).

    **How does autologging work for meta estimators?**
      When a meta estimator (e.g. `Pipeline`_, `GridSearchCV`_) calls ``fit()``, it internally calls
      ``fit()`` on its child estimators. Autologging does NOT perform logging on these constituent
      ``fit()`` calls.

      **Parameter search**
          In addition to recording the information discussed above, autologging for parameter
          search meta estimators (`GridSearchCV`_ and `RandomizedSearchCV`_) records child runs
          with metrics for each set of explored parameters, as well as artifacts and parameters
          for the best model (if available).

    **Supported estimators**
      - All estimators obtained by `sklearn.utils.all_estimators`_ (including meta estimators).
      - `Pipeline`_
      - Parameter search estimators (`GridSearchCV`_ and `RandomizedSearchCV`_)

    .. _sklearn.utils.all_estimators:
        https://scikit-learn.org/stable/modules/generated/sklearn.utils.all_estimators.html

    .. _Pipeline:
        https://scikit-learn.org/stable/modules/generated/sklearn.pipeline.Pipeline.html

    .. _GridSearchCV:
        https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.GridSearchCV.html

    .. _RandomizedSearchCV:
        https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.RandomizedSearchCV.html

    **Example**

    `See more examples <https://github.com/mlflow/mlflow/blob/master/examples/sklearn_autolog>`_

    .. code-block:: python

        from pprint import pprint
        import numpy as np
        from sklearn.linear_model import LinearRegression
        import mlflow

        def fetch_logged_data(run_id):
            client = mlflow.tracking.MlflowClient()
            data = client.get_run(run_id).data
            tags = {k: v for k, v in data.tags.items() if not k.startswith("mlflow.")}
            artifacts = [f.path for f in client.list_artifacts(run_id, "model")]
            return data.params, data.metrics, tags, artifacts

        # enable autologging
        mlflow.sklearn.autolog()

        # prepare training data
        X = np.array([[1, 1], [1, 2], [2, 2], [2, 3]])
        y = np.dot(X, np.array([1, 2])) + 3

        # train a model
        model = LinearRegression()
        with mlflow.start_run() as run:
            model.fit(X, y)

        # fetch logged data
        params, metrics, tags, artifacts = fetch_logged_data(run.info.run_id)

        pprint(params)
        # {'copy_X': 'True',
        #  'fit_intercept': 'True',
        #  'n_jobs': 'None',
        #  'normalize': 'False'}

        pprint(metrics)
        # {'training_score': 1.0}

        pprint(tags)
        # {'estimator_class': 'sklearn.linear_model._base.LinearRegression',
        #  'estimator_name': 'LinearRegression'}

        pprint(artifacts)
        # ['model/MLmodel', 'model/conda.yaml', 'model/model.pkl']
    """
    import pandas as pd
    import sklearn

    from mlflow.models import infer_signature
    from mlflow.sklearn.utils import (
        _MIN_SKLEARN_VERSION,
        _is_supported_version,
        _chunk_dict,
        _get_args_for_score,
        _get_Xy,
        _all_estimators,
        _truncate_dict,
        _get_arg_names,
        _get_estimator_info_tags,
        _get_meta_estimators_for_autologging,
        _is_parameter_search_estimator,
        _log_parameter_search_results_as_artifact,
        _create_child_runs_for_parameter_search,
    )
    from mlflow.tracking.context import registry as context_registry
    from mlflow.utils.validation import (
        MAX_PARAMS_TAGS_PER_BATCH,
        MAX_PARAM_VAL_LENGTH,
        MAX_ENTITY_KEY_LENGTH,
    )

    if not _is_supported_version():
        warnings.warn(
            "Autologging utilities may not work properly on scikit-learn < {} ".format(
                _MIN_SKLEARN_VERSION
            )
            + "(current version: {})".format(sklearn.__version__),
            stacklevel=2,
        )

    def fit_mlflow(self, func_name, *args, **kwargs):
        should_start_run = mlflow.active_run() is None
        if should_start_run:
            try_mlflow_log(mlflow.start_run)

        _log_pretraining_metadata(self, *args, **kwargs)

        original_fit = gorilla.get_original_attribute(self, func_name)
        try:
            fit_output = original_fit(*args, **kwargs)
        except Exception as e:
            if should_start_run:
                try_mlflow_log(mlflow.end_run, RunStatus.to_string(RunStatus.FAILED))

            raise e

        _log_posttraining_metadata(self, *args, **kwargs)

        if should_start_run:
            try_mlflow_log(mlflow.end_run)

        return fit_output

    def _log_pretraining_metadata(estimator, *args, **kwargs):  # pylint: disable=unused-argument
        """
        Records metadata (e.g., params and tags) for a scikit-learn estimator prior to training.
        This is intended to be invoked within a patched scikit-learn training routine
        (e.g., `fit()`, `fit_transform()`, ...) and assumes the existence of an active
        MLflow run that can be referenced via the fluent Tracking API.

        :param estimator: The scikit-learn estimator for which to log metadata.
        :param args: The arguments passed to the scikit-learn training routine (e.g.,
                     `fit()`, `fit_transform()`, ...).
        :param kwargs: The keyword arguments passed to the scikit-learn training routine.
        """
        # Deep parameter logging includes parameters from children of a given
        # estimator. For some meta estimators (e.g., pipelines), recording
        # these parameters is desirable. For parameter search estimators,
        # however, child estimators act as seeds for the parameter search
        # process; accordingly, we avoid logging initial, untuned parameters
        # for these seed estimators.
        should_log_params_deeply = not _is_parameter_search_estimator(estimator)
        # Chunk model parameters to avoid hitting the log_batch API limit
        for chunk in _chunk_dict(
            estimator.get_params(deep=should_log_params_deeply),
            chunk_size=MAX_PARAMS_TAGS_PER_BATCH,
        ):
            truncated = _truncate_dict(chunk, MAX_ENTITY_KEY_LENGTH, MAX_PARAM_VAL_LENGTH)
            try_mlflow_log(mlflow.log_params, truncated)

        try_mlflow_log(mlflow.set_tags, _get_estimator_info_tags(estimator))

    def _log_posttraining_metadata(estimator, *args, **kwargs):
        """
        Records metadata for a scikit-learn estimator after training has completed.
        This is intended to be invoked within a patched scikit-learn training routine
        (e.g., `fit()`, `fit_transform()`, ...) and assumes the existence of an active
        MLflow run that can be referenced via the fluent Tracking API.

        :param estimator: The scikit-learn estimator for which to log metadata.
        :param args: The arguments passed to the scikit-learn training routine (e.g.,
                     `fit()`, `fit_transform()`, ...).
        :param kwargs: The keyword arguments passed to the scikit-learn training routine.
        """
        if hasattr(estimator, "score"):
            try:
                score_args = _get_args_for_score(estimator.score, estimator.fit, args, kwargs)
                training_score = estimator.score(*score_args)
            except Exception as e:  # pylint: disable=broad-except
                msg = (
                    estimator.score.__qualname__
                    + " failed. The 'training_score' metric will not be recorded. Scoring error: "
                    + str(e)
                )
                _logger.warning(msg)
            else:
                try_mlflow_log(mlflow.log_metric, "training_score", training_score)

        input_example = None
        signature = None
        if hasattr(estimator, "predict"):
            try:
                # Fetch an input example using the first several rows of the array-like
                # training data supplied to the training routine (e.g., `fit()`)
                SAMPLE_ROWS = 5
                fit_arg_names = _get_arg_names(estimator.fit)
                X_var_name, y_var_name = fit_arg_names[:2]
                input_example = _get_Xy(args, kwargs, X_var_name, y_var_name)[0][:SAMPLE_ROWS]

                model_output = estimator.predict(input_example)
                signature = infer_signature(input_example, model_output)
            except Exception as e:  # pylint: disable=broad-except
                input_example = None
                msg = "Failed to infer an input example and model signature: " + str(e)
                _logger.warning(msg)

        try_mlflow_log(
            log_model,
            estimator,
            artifact_path="model",
            signature=signature,
            input_example=input_example,
        )

        if _is_parameter_search_estimator(estimator):
            if hasattr(estimator, "best_estimator_"):
                try_mlflow_log(
                    log_model,
                    estimator.best_estimator_,
                    artifact_path="best_estimator",
                    signature=signature,
                    input_example=input_example,
                )

            if hasattr(estimator, "best_params_"):
                best_params = {
                    "best_{param_name}".format(param_name=param_name): param_value
                    for param_name, param_value in estimator.best_params_.items()
                }
                try_mlflow_log(mlflow.log_params, best_params)

            if hasattr(estimator, "cv_results_"):
                try:
                    # Fetch environment-specific tags (e.g., user and source) to ensure that lineage
                    # information is consistent with the parent run
                    environment_tags = context_registry.resolve_tags()
                    _create_child_runs_for_parameter_search(
                        cv_estimator=estimator,
                        parent_run=mlflow.active_run(),
                        child_tags=environment_tags,
                    )
                except Exception as e:  # pylint: disable=broad-except

                    msg = (
                        "Encountered exception during creation of child runs for parameter search."
                        " Child runs may be missing. Exception: {}".format(str(e))
                    )
                    _logger.warning(msg)

                try:
                    cv_results_df = pd.DataFrame.from_dict(estimator.cv_results_)
                    _log_parameter_search_results_as_artifact(
                        cv_results_df, mlflow.active_run().info.run_id
                    )
                except Exception as e:  # pylint: disable=broad-except

                    msg = (
                        "Failed to log parameter search results as an artifact."
                        " Exception: {}".format(str(e))
                    )
                    _logger.warning(msg)

    def patched_fit(self, func_name, *args, **kwargs):
        """
        To be applied to a sklearn model class that defines a `fit` method and
        inherits from `BaseEstimator` (thereby defining the `get_params()` method)
        """
        with _SklearnTrainingSession(clazz=self.__class__, allow_children=False) as t:
            if t.should_log():
                return fit_mlflow(self, func_name, *args, **kwargs)
            else:
                original_fit = gorilla.get_original_attribute(self, func_name)
                return original_fit(*args, **kwargs)

    def create_patch_func(func_name):
        def f(self, *args, **kwargs):
            return patched_fit(self, func_name, *args, **kwargs)

        return f

    patch_settings = gorilla.Settings(allow_hit=True, store_hit=True)
    _, estimators_to_patch = zip(*_all_estimators())
    # Ensure that relevant meta estimators (e.g. GridSearchCV, Pipeline) are selected
    # for patching if they are not already included in the output of `all_estimators()`
    estimators_to_patch = set(estimators_to_patch).union(
        set(_get_meta_estimators_for_autologging())
    )
    for class_def in estimators_to_patch:
        for func_name in ["fit", "fit_transform", "fit_predict"]:
            if hasattr(class_def, func_name):
                original = getattr(class_def, func_name)

                # A couple of estimators use property methods to return fitting functions,
                # rather than defining the fitting functions on the estimator class directly.
                #
                # Example: https://github.com/scikit-learn/scikit-learn/blob/0.23.2/sklearn/neighbors/_lof.py#L183  # noqa
                #
                # We currently exclude these property fitting methods from patching because
                # it's challenging to patch them correctly.
                #
                # Excluded fitting methods:
                # - sklearn.cluster._agglomerative.FeatureAgglomeration.fit_predict
                # - sklearn.neighbors._lof.LocalOutlierFactor.fit_predict
                #
                # You can list property fitting methods by inserting "print(class_def, func_name)"
                # in the if clause below.
                if isinstance(original, property):
                    continue

                patch_func = create_patch_func(func_name)
                # TODO(harupy): Package this wrap & patch routine into a utility function so we can
                # reuse it in other autologging integrations.
                # preserve original function attributes
                patch_func = functools.wraps(original)(patch_func)
                patch = gorilla.Patch(class_def, func_name, patch_func, settings=patch_settings)
                gorilla.apply(patch)
