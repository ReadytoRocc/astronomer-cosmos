# mypy: ignore-errors
# ignoring enum Mypy errors

from __future__ import annotations

import copy
import inspect
from typing import Any, Callable
from warnings import warn

from airflow.models.dag import DAG
from airflow.utils.dag_parsing_context import get_parsing_context
from airflow.utils.task_group import TaskGroup

from cosmos.airflow.graph import build_airflow_graph
from cosmos.config import ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
from cosmos.constants import ExecutionMode
from cosmos.dbt.graph import DbtGraph
from cosmos.dbt.selector import retrieve_by_label
from cosmos.exceptions import CosmosValueError
from cosmos.log import get_logger

logger = get_logger(__name__)


def migrate_to_new_interface(
    execution_config: ExecutionConfig, project_config: ProjectConfig, render_config: RenderConfig
):
    # We copy the configuration so the change does not affect other DAGs or TaskGroups
    # that may reuse the same original configuration
    render_config = copy.deepcopy(render_config)
    execution_config = copy.deepcopy(execution_config)
    render_config.project_path = project_config.dbt_project_path
    execution_config.project_path = project_config.dbt_project_path
    return execution_config, render_config


def specific_kwargs(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """
    Extract kwargs specific to the cosmos.converter.DbtToAirflowConverter class initialization method.

    :param kwargs: kwargs which can contain DbtToAirflowConverter and non DbtToAirflowConverter kwargs.
    """
    new_kwargs = {}
    specific_args_keys = inspect.getfullargspec(DbtToAirflowConverter.__init__).args
    for arg_key, arg_value in kwargs.items():
        if arg_key in specific_args_keys:
            new_kwargs[arg_key] = arg_value
    return new_kwargs


def airflow_kwargs(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """
    Extract kwargs specific to the Airflow DAG or TaskGroup class initialization method.

    :param kwargs: kwargs which can contain Airflow DAG or TaskGroup and cosmos.converter.DbtToAirflowConverter kwargs.
    """
    new_kwargs = {}
    non_airflow_kwargs = specific_kwargs(**kwargs)
    for arg_key, arg_value in kwargs.items():
        if arg_key not in non_airflow_kwargs or arg_key == "dag":
            new_kwargs[arg_key] = arg_value
    return new_kwargs


def validate_arguments(
    select: list[str],
    exclude: list[str],
    profile_config: ProfileConfig,
    task_args: dict[str, Any],
    execution_mode: ExecutionMode,
) -> None:
    """
    Validate that mutually exclusive selectors filters have not been given.
    Validate deprecated arguments.

    :param select: A list of dbt select arguments (e.g. 'config.materialized:incremental')
    :param exclude: A list of dbt exclude arguments (e.g. 'tag:nightly')
    :param profile_config: ProfileConfig Object
    :param task_args: Arguments to be used to instantiate an Airflow Task
    :param execution_mode: the current execution mode
    """
    for field in ("tags", "paths"):
        select_items = retrieve_by_label(select, field)
        exclude_items = retrieve_by_label(exclude, field)
        intersection = {str(item) for item in set(select_items).intersection(exclude_items)}
        if intersection:
            raise CosmosValueError(f"Can't specify the same {field[:-1]} in `select` and `exclude`: " f"{intersection}")

    # if task_args has a schema, add it to the profile args and add a deprecated warning
    if "schema" in task_args:
        logger.warning("Specifying a schema in the `task_args` is deprecated. Please use the `profile_args` instead.")
        if profile_config.profile_mapping:
            profile_config.profile_mapping.profile_args["schema"] = task_args["schema"]

    if execution_mode in [ExecutionMode.LOCAL, ExecutionMode.VIRTUALENV]:
        profile_config.validate_profiles_yml()


def validate_initial_user_config(
    execution_config: ExecutionConfig,
    profile_config: ProfileConfig | None,
    project_config: ProjectConfig,
    render_config: RenderConfig,
    operator_args: dict[str, Any],
):
    """
    Validates if the user set the fields as expected.

    :param execution_config: Configuration related to how to run dbt in Airflow tasks
    :param profile_config: Configuration related to dbt database configuration (profile)
    :param project_config: Configuration related to the overall dbt project
    :param render_config: Configuration related to how to convert the dbt workflow into an Airflow DAG
    :param operator_args: Arguments to pass to the underlying operators.
    """
    if profile_config is None and execution_config.execution_mode not in (
        ExecutionMode.KUBERNETES,
        ExecutionMode.DOCKER,
    ):
        raise CosmosValueError(f"The profile_config is mandatory when using {execution_config.execution_mode}")

    # Since we now support both project_config.dbt_project_path, render_config.project_path and execution_config.project_path
    # We need to ensure that only one interface is being used.
    if project_config.dbt_project_path and (render_config.project_path or execution_config.project_path):
        raise CosmosValueError(
            "ProjectConfig.dbt_project_path is mutually exclusive with RenderConfig.dbt_project_path and ExecutionConfig.dbt_project_path."
            + "If using RenderConfig.dbt_project_path or ExecutionConfig.dbt_project_path, ProjectConfig.dbt_project_path should be None"
        )

    # Cosmos 2.0 will remove the ability to pass in operator_args with 'env' and 'vars' in place of ProjectConfig.env_vars and
    # ProjectConfig.dbt_vars.
    if "env" in operator_args:
        warn(
            "operator_args with 'env' is deprecated since Cosmos 1.3 and will be removed in Cosmos 2.0. Use ProjectConfig.env_vars instead.",
            DeprecationWarning,
        )
        if project_config.env_vars:
            raise CosmosValueError(
                "ProjectConfig.env_vars and operator_args with 'env' are mutually exclusive and only one can be used."
            )
    if "vars" in operator_args:
        warn(
            "operator_args with 'vars' is deprecated since Cosmos 1.3 and will be removed in Cosmos 2.0. Use ProjectConfig.vars instead.",
            DeprecationWarning,
        )
        if project_config.dbt_vars:
            raise CosmosValueError(
                "ProjectConfig.dbt_vars and operator_args with 'vars' are mutually exclusive and only one can be used."
            )
    # Cosmos 2.0 will remove the ability to pass RenderConfig.env_vars in place of ProjectConfig.env_vars, check that both are not set.
    if project_config.env_vars and render_config.env_vars:
        raise CosmosValueError(
            "Both ProjectConfig.env_vars and RenderConfig.env_vars were provided. RenderConfig.env_vars is deprecated since Cosmos 1.3, "
            "please use ProjectConfig.env_vars instead."
        )


def validate_adapted_user_config(
    execution_config: ExecutionConfig | None, project_config: ProjectConfig, render_config: RenderConfig | None
):
    """
    Validates if all the necessary fields required by Cosmos to render the DAG are set.

    :param execution_config: Configuration related to how to run dbt in Airflow tasks
    :param project_config: Configuration related to the overall dbt project
    :param render_config: Configuration related to how to convert the dbt workflow into an Airflow DAG
    """
    # At this point, execution_config.project_path should always be non-null
    if not execution_config.project_path:
        raise CosmosValueError(
            "ExecutionConfig.dbt_project_path is required for the execution of dbt tasks in all execution modes."
        )

    # We now have a guaranteed execution_config.project_path, but still need to process render_config.project_path
    # We require render_config.project_path when we dont have a manifest
    if not project_config.manifest_path and not render_config.project_path:
        raise CosmosValueError(
            "RenderConfig.dbt_project_path is required for rendering an airflow DAG from a DBT Graph if no manifest is provided."
        )


class DbtToAirflowConverter:
    """
    Logic common to build an Airflow DbtDag and DbtTaskGroup from a DBT project.

    :param dag: Airflow DAG to be populated
    :param task_group (optional): Airflow Task Group to be populated
    :param project_config: The dbt project configuration
    :param execution_config: The dbt execution configuration
    :param render_config: The dbt render configuration
    :param operator_args: Parameters to pass to the underlying operators, can include KubernetesPodOperator
        or DockerOperator parameters
    :param on_warning_callback: A callback function called on warnings with additional Context variables "test_names"
        and "test_results" of type `List`. Each index in "test_names" corresponds to the same index in "test_results".
    :param parse_all_dags: When False, only the DAG that matches the provided dag_id will be parsed. When True, all DAGs
        will be parsed. Default is False.
    """

    def __init__(
        self,
        project_config: ProjectConfig,
        profile_config: ProfileConfig | None = None,
        execution_config: ExecutionConfig | None = None,
        render_config: RenderConfig | None = None,
        dag: DAG | None = None,
        task_group: TaskGroup | None = None,
        operator_args: dict[str, Any] | None = None,
        on_warning_callback: Callable[..., Any] | None = None,
        parse_all_dags: bool = False,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if not dag and task_group:
            dag = task_group.dag

        if not dag:
            raise CosmosValueError("Either a dag or task_group must be provided.")

        if not parse_all_dags:
            current_dag_id = get_parsing_context().dag_id

            if dag.dag_id != current_dag_id and current_dag_id is not None:
                return

        # if the dag is not what we're trying to parse, we can skip for performance reasons
        # https://airflow.apache.org/docs/apache-airflow/stable/howto/dynamic-dag-generation.html#optimizing-dag-parsing-delays-during-execution

        project_config.validate_project()

        execution_config = execution_config or ExecutionConfig()
        render_config = render_config or RenderConfig()
        operator_args = operator_args or {}

        validate_initial_user_config(execution_config, profile_config, project_config, render_config, operator_args)

        # If we are using the old interface, we should migrate it to the new interface
        # This is safe to do now since we have validated which config interface we're using
        if project_config.dbt_project_path:
            execution_config, render_config = migrate_to_new_interface(execution_config, project_config, render_config)

        validate_adapted_user_config(execution_config, project_config, render_config)

        env_vars = project_config.env_vars or operator_args.get("env")
        dbt_vars = project_config.dbt_vars or operator_args.get("vars")

        # Previously, we were creating a cosmos.dbt.project.DbtProject
        # DbtProject has now been replaced with ProjectConfig directly
        #   since the interface of the two classes were effectively the same
        # Under this previous implementation, we were passing:
        #  - name, root dir, models dir, snapshots dir and manifest path
        # Internally in the dbtProject class, we were defaulting the profile_path
        #   To be root dir/profiles.yml
        # To keep this logic working, if converter is given no ProfileConfig,
        #   we can create a default retaining this value to preserve this functionality.
        # We may want to consider defaulting this value in our actual ProjceConfig class?
        self.dbt_graph = DbtGraph(
            project=project_config,
            render_config=render_config,
            execution_config=execution_config,
            profile_config=profile_config,
            dbt_vars=dbt_vars,
        )
        self.dbt_graph.load(method=render_config.load_method, execution_mode=execution_config.execution_mode)

        task_args = {
            **operator_args,
            "project_dir": execution_config.project_path,
            "partial_parse": project_config.partial_parse,
            "profile_config": profile_config,
            "emit_datasets": render_config.emit_datasets,
            "env": env_vars,
            "vars": dbt_vars,
        }
        if execution_config.dbt_executable_path:
            task_args["dbt_executable_path"] = execution_config.dbt_executable_path
        if execution_config.invocation_mode:
            task_args["invocation_mode"] = execution_config.invocation_mode

        validate_arguments(
            render_config.select,
            render_config.exclude,
            profile_config,
            task_args,
            execution_mode=execution_config.execution_mode,
        )

        build_airflow_graph(
            nodes=self.dbt_graph.filtered_nodes,
            dag=dag or (task_group and task_group.dag),
            task_group=task_group,
            execution_mode=execution_config.execution_mode,
            task_args=task_args,
            test_indirect_selection=execution_config.test_indirect_selection,
            dbt_project_name=project_config.project_name,
            on_warning_callback=on_warning_callback,
            render_config=render_config,
        )
