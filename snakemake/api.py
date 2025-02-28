__author__ = "Johannes Köster"
__copyright__ = "Copyright 2022, Johannes Köster"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Dict, List, Optional, Set
import os
from functools import partial
import importlib

from snakemake.common import MIN_PY_VERSION, SNAKEFILE_CHOICES
from snakemake.settings import (
    ChangeType,
    GroupSettings,
    SchedulingSettings,
    WorkflowSettings,
)

if sys.version_info < MIN_PY_VERSION:
    raise ValueError(f"Snakemake requires at least Python {'.'.join(MIN_PY_VERSION)}.")

from snakemake.common.workdir_handler import WorkdirHandler
from snakemake.settings import (
    DAGSettings,
    DeploymentMethod,
    DeploymentSettings,
    ExecutionSettings,
    OutputSettings,
    ConfigSettings,
    RemoteExecutionSettings,
    ResourceSettings,
    StorageSettings,
)

from snakemake_interface_executor_plugins.settings import ExecMode
from snakemake_interface_executor_plugins import ExecutorSettingsBase
from snakemake_interface_executor_plugins.registry import ExecutorPluginRegistry
from snakemake_interface_common.exceptions import ApiError

from snakemake.workflow import Workflow
from snakemake.exceptions import print_exception
from snakemake.logging import setup_logger, logger
from snakemake.shell import shell
from snakemake.common import (
    MIN_PY_VERSION,
    __version__,
)
from snakemake.resources import DefaultResources


class ApiBase(ABC):
    def __post_init__(self):
        self._check()

    def _check(self):
        # nothing to check by default
        # override in subclasses if needed
        pass


def resolve_snakefile(path: Optional[Path]):
    """Get path to the snakefile.

    Arguments
    ---------
    path: Optional[Path] -- The path to the snakefile. If not provided, default locations will be tried.
    """
    if path is None:
        for p in SNAKEFILE_CHOICES:
            if p.exists():
                return p
        raise ApiError(
            f"No Snakefile found, tried {', '.join(map(str, SNAKEFILE_CHOICES))}."
        )
    return path


@dataclass
class SnakemakeApi(ApiBase):
    """The Snakemake API.

    Arguments
    ---------

    output_settings: OutputSettings -- The output settings for the Snakemake API.
    """

    output_settings: OutputSettings = field(default_factory=OutputSettings)
    _workflow_api: Optional["WorkflowApi"] = field(init=False, default=None)
    _is_in_context: bool = field(init=False, default=False)

    def workflow(
        self,
        resource_settings: ResourceSettings,
        config_settings: Optional[ConfigSettings] = None,
        storage_settings: Optional[StorageSettings] = None,
        workflow_settings: Optional[WorkflowSettings] = None,
        snakefile: Optional[Path] = None,
        workdir: Optional[Path] = None,
    ):
        """Create the workflow API.

        Note that if provided, this also changes to the provided workdir.
        It will change back to the previous working directory when the workflow API object is deleted.

        Arguments
        ---------
        config_settings: ConfigSettings -- The config settings for the workflow.
        resource_settings: ResourceSettings -- The resource settings for the workflow.
        storage_settings: StorageSettings -- The storage settings for the workflow.
        snakefile: Optional[Path] -- The path to the snakefile. If not provided, default locations will be tried.
        workdir: Optional[Path] -- The path to the working directory. If not provided, the current working directory will be used.
        """

        if config_settings is None:
            config_settings = ConfigSettings()
        if storage_settings is None:
            storage_settings = StorageSettings()
        if workflow_settings is None:
            workflow_settings = WorkflowSettings()

        self._check_is_in_context()

        self._setup_logger()

        snakefile = resolve_snakefile(snakefile)

        self._workflow_api = WorkflowApi(
            snakemake_api=self,
            snakefile=snakefile,
            workdir=workdir,
            config_settings=config_settings,
            resource_settings=resource_settings,
            storage_settings=storage_settings,
            workflow_settings=workflow_settings,
        )
        return self._workflow_api

    def _cleanup(self):
        """Cleanup the workflow."""
        if not self.output_settings.keep_logger:
            logger.cleanup()
        if self._workflow_api is not None:
            self._workflow_api._workdir_handler.change_back()
            if (
                self._workflow_api._workflow_store is not None
                and self._workflow_api._workflow._workdir_handler is not None
            ):
                self._workflow_api._workflow._workdir_handler.change_back()

    def print_exception(self, ex: Exception):
        """Print an exception during workflow execution in a human readable way
        (with adjusted line numbers for exceptions raised in Snakefiles and stack
        traces that hide Snakemake internals for better readability).

        Arguments
        ---------
        ex: Exception -- The exception to print.
        """
        linemaps = (
            self._workflow_api._workflow.linemaps
            if self._workflow_api is not None
            else dict()
        )
        print_exception(ex, linemaps)

    def _setup_logger(
        self,
        stdout: bool = False,
        mode: ExecMode = ExecMode.DEFAULT,
        dryrun: bool = False,
    ):
        if not self.output_settings.keep_logger:
            setup_logger(
                handler=self.output_settings.log_handlers,
                quiet=self.output_settings.quiet,
                nocolor=self.output_settings.nocolor,
                debug=self.output_settings.verbose,
                printshellcmds=self.output_settings.printshellcmds,
                debug_dag=self.output_settings.debug_dag,
                stdout=stdout,
                mode=mode,
                show_failed_logs=self.output_settings.show_failed_logs,
                dryrun=dryrun,
            )

    def _check_is_in_context(self):
        if not self._is_in_context:
            raise ApiError(
                "This method can only be called when SnakemakeApi is used within a with "
                "statement."
            )

    def __enter__(self):
        self._is_in_context = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._is_in_context = False
        self._cleanup()


@dataclass
class WorkflowApi(ApiBase):
    """The workflow API.

    Arguments
    ---------
    snakemake_api: SnakemakeApi -- The Snakemake API.
    snakefile: Path -- The path to the snakefile.
    config_settings: ConfigSettings -- The config settings for the workflow.
    resource_settings: ResourceSettings -- The resource settings for the workflow.
    """

    snakemake_api: SnakemakeApi
    snakefile: Path
    workdir: Optional[Path]
    config_settings: ConfigSettings
    resource_settings: ResourceSettings
    storage_settings: StorageSettings
    workflow_settings: WorkflowSettings
    _workflow_store: Optional[Workflow] = field(init=False, default=None)
    _workdir_handler: Optional[WorkdirHandler] = field(init=False)

    def dag(
        self,
        dag_settings: Optional[DAGSettings] = None,
        deployment_settings: Optional[DeploymentSettings] = None,
    ):
        """Create a DAG API.

        Arguments
        ---------
        dag_settings: DAGSettings -- The DAG settings for the DAG API.
        """
        if dag_settings is None:
            dag_settings = DAGSettings()
        if deployment_settings is None:
            deployment_settings = DeploymentSettings()

        return DAGApi(
            self.snakemake_api,
            self,
            dag_settings=dag_settings,
            deployment_settings=deployment_settings,
        )

    def lint(self, json: bool = False):
        """Lint the workflow.

        Arguments
        ---------
        json: bool -- Whether to print the linting results as JSON.

        Returns
        -------
        True if any lints were printed
        """
        workflow = self._get_workflow(check_envvars=False)
        workflow.include(
            self.snakefile, overwrite_default_target=True, print_compilation=False
        )
        workflow.check()
        return workflow.lint(json=json)

    def list_rules(self, only_targets: bool = False):
        """List the rules of the workflow.

        Arguments
        ---------
        only_targets: bool -- Whether to only list target rules.
        """
        self._workflow.list_rules(only_targets=only_targets)

    def list_resources(self):
        """List the resources of the workflow."""
        self._workflow.list_resources()

    def print_compilation(self):
        """Print the pure python compilation of the workflow."""
        workflow = self._get_workflow()
        workflow.include(self.snakefile, print_compilation=True)

    @property
    def _workflow(self):
        if self._workflow_store is None:
            workflow = self._get_workflow()
            workflow.include(
                self.snakefile, overwrite_default_target=True, print_compilation=False
            )
            workflow.check()
            self._workflow_store = workflow
        return self._workflow_store

    def _get_workflow(self, **kwargs):
        from snakemake.workflow import Workflow

        return Workflow(
            config_settings=self.config_settings,
            resource_settings=self.resource_settings,
            workflow_settings=self.workflow_settings,
            storage_settings=self.storage_settings,
            output_settings=self.snakemake_api.output_settings,
            overwrite_workdir=self.workdir,
            **kwargs,
        )

    def __post_init__(self):
        super().__post_init__()
        self.snakefile = self.snakefile.absolute()
        self._workdir_handler = WorkdirHandler(self.workdir)
        self._workdir_handler.change_to()

    def _check(self):
        if not self.snakefile.exists():
            raise ApiError(f'Snakefile "{self.snakefile}" not found.')


@dataclass
class DAGApi(ApiBase):
    """The DAG API.

    Arguments
    ---------
    snakemake_api: SnakemakeApi -- The Snakemake API.
    workflow_api: WorkflowApi -- The workflow API.
    dag_settings: DAGSettings -- The DAG settings for the DAG API.
    """

    snakemake_api: SnakemakeApi
    workflow_api: WorkflowApi
    dag_settings: DAGSettings
    deployment_settings: DeploymentSettings

    def __post_init__(self):
        self.workflow_api._workflow.dag_settings = self.dag_settings
        self.workflow_api._workflow.deployment_settings = self.deployment_settings

    def execute_workflow(
        self,
        executor: str = "local",
        execution_settings: Optional[ExecutionSettings] = None,
        remote_execution_settings: Optional[RemoteExecutionSettings] = None,
        scheduling_settings: Optional[SchedulingSettings] = None,
        group_settings: Optional[GroupSettings] = None,
        executor_settings: Optional[ExecutorSettingsBase] = None,
        updated_files: Optional[List[str]] = None,
    ):
        """Execute the workflow.

        Arguments
        ---------
        executor: str -- The executor to use.
        execution_settings: ExecutionSettings -- The execution settings for the workflow.
        resource_settings: ResourceSettings -- The resource settings for the workflow.
        deployment_settings: DeploymentSettings -- The deployment settings for the workflow.
        remote_execution_settings: RemoteExecutionSettings -- The remote execution settings for the workflow.
        executor_settings: Optional[ExecutorSettingsBase] -- The executor settings for the workflow.
        updated_files: Optional[List[str]] -- An optional list where Snakemake will put all updated files.
        """

        if execution_settings is None:
            execution_settings = ExecutionSettings()
        if remote_execution_settings is None:
            remote_execution_settings = RemoteExecutionSettings()
        if scheduling_settings is None:
            scheduling_settings = SchedulingSettings()
        if group_settings is None:
            group_settings = GroupSettings()

        if (
            remote_execution_settings.immediate_submit
            and not self.workflow_api.storage_settings.notemp
        ):
            raise ApiError(
                "immediate_submit has to be combined with notemp (it does not support temp file handling)"
            )

        executor_plugin_registry = _get_executor_plugin_registry()
        executor_plugin = executor_plugin_registry.get_plugin(executor)

        if executor_plugin.common_settings.implies_no_shared_fs:
            self.workflow_api.storage_settings.assume_shared_fs = False

        self.snakemake_api._setup_logger(
            stdout=executor_plugin.common_settings.dryrun_exec,
            mode=execution_settings.mode,
            dryrun=executor_plugin.common_settings.dryrun_exec,
        )

        if executor_plugin.common_settings.local_exec:
            if (
                not executor_plugin.common_settings.dryrun_exec
                and not executor_plugin.common_settings.touch_exec
            ):
                if self.workflow_api.resource_settings.cores is None:
                    raise ApiError(
                        "cores have to be specified for local execution "
                        "(use --cores N with N being a number >= 1 or 'all')"
                    )
                # clean up all previously recorded jobids.
                shell.cleanup()
            else:
                # set cores if that is not done yet
                if self.workflow_api.resource_settings.cores is None:
                    self.workflow_api.resource_settings.cores = 1
            if (
                execution_settings.debug
                and self.workflow_api.resource_settings.cores > 1
            ):
                raise ApiError(
                    "debug mode cannot be used with multi-core execution, "
                    "please enforce a single core by setting --cores 1"
                )
        else:
            if self.workflow_api.resource_settings.nodes is None:
                raise ApiError(
                    "maximum number of parallel jobs/used nodes has to be specified for remote execution "
                    "(use --jobs N with N being a number >= 1)"
                )
            # non local execution
            if self.workflow_api.resource_settings.default_resources is None:
                # use full default resources if in cluster or cloud mode
                self.workflow_api.resource_settings.default_resources = (
                    DefaultResources(mode="full")
                )
            if execution_settings.edit_notebook is not None:
                raise ApiError(
                    "notebook edit mode is only allowed with local execution."
                )
            if execution_settings.debug:
                raise ApiError("debug mode cannot be used with non-local execution")

        execution_settings.use_threads = (
            execution_settings.use_threads
            or (os.name not in ["posix"])
            or not executor_plugin.common_settings.local_exec
        )

        logger.setup_logfile()

        workflow = self.workflow_api._workflow
        workflow.execution_settings = execution_settings
        workflow.remote_execution_settings = remote_execution_settings
        workflow.scheduling_settings = scheduling_settings
        workflow.group_settings = group_settings

        workflow.execute(
            executor_plugin=executor_plugin,
            executor_settings=executor_settings,
            updated_files=updated_files,
        )

    def generate_unit_tests(self, path: Path):
        """Generate unit tests for the workflow.

        Arguments
        ---------
        path: Path -- The path to store the unit tests.
        """
        self.workflow_api._workflow.generate_unit_tests(path=path)

    def containerize(self):
        """Containerize the workflow."""
        self.workflow_api._workflow.containerize()

    def create_report(
        self,
        path: Path,
        stylesheet: Optional[Path] = None,
    ):
        """Create a report for the workflow.

        Arguments
        ---------
        report: Path -- The path to the report.
        report_stylesheet: Optional[Path] -- The path to the report stylesheet.
        """
        self.workflow_api._workflow.create_report(
            path=path,
            stylesheet=stylesheet,
        )

    def printdag(self):
        """Print the DAG of the workflow."""
        self.workflow_api._workflow.printdag()

    def printrulegraph(self):
        """Print the rule graph of the workflow."""
        self.workflow_api._workflow.printrulegraph()

    def printfilegraph(self):
        """Print the file graph of the workflow."""
        self.workflow_api._workflow.printfilegraph()

    def printd3dag(self):
        """Print the DAG of the workflow in D3.js compatible JSON."""
        self.workflow_api._workflow.printd3dag()

    def unlock(self):
        """Unlock the workflow."""
        self.workflow_api._workflow.unlock()

    def cleanup_metadata(self, paths: List[Path]):
        """Cleanup the metadata of the workflow."""
        self.workflow_api._workflow.cleanup_metadata(paths)

    def conda_cleanup_envs(self):
        """Cleanup the conda environments of the workflow."""
        self.deployment_settings.imply_deployment_method(DeploymentMethod.CONDA)
        self.workflow_api._workflow.conda_cleanup_envs()

    def conda_create_envs(self):
        """Only create the conda environments of the workflow."""
        self.deployment_settings.imply_deployment_method(DeploymentMethod.CONDA)
        self.workflow_api._workflow.conda_create_envs()

    def conda_list_envs(self):
        """List the conda environments of the workflow."""
        self.deployment_settings.imply_deployment_method(DeploymentMethod.CONDA)
        self.workflow_api._workflow.conda_list_envs()

    def cleanup_shadow(self):
        """Cleanup the shadow directories of the workflow."""
        self.workflow_api._workflow.cleanup_shadow()

    def container_cleanup_images(self):
        """Cleanup the container images of the workflow."""
        self.deployment_settings.imply_deployment_method(DeploymentMethod.APPTAINER)
        self.workflow_api._workflow.container_cleanup_images()

    def list_changes(self, change_type: ChangeType):
        """List the changes of the workflow.

        Arguments
        ---------
        change_type: ChangeType -- The type of changes to list.
        """
        self.workflow_api._workflow.list_changes(change_type=change_type)

    def list_untracked(self):
        """List the untracked files of the workflow."""
        self.workflow_api._workflow.list_untracked()

    def summary(self, detailed: bool = False):
        """Summarize the workflow.

        Arguments
        ---------
        detailed: bool -- Whether to print a detailed summary.
        """
        self.workflow_api._workflow.summary(detailed=detailed)

    def archive(self, path: Path):
        """Archive the workflow.

        Arguments
        ---------
        path: Path -- The path to the archive.
        """
        self.workflow_api._workflow.archive(path=path)

    def delete_output(self, only_temp: bool = False, dryrun: bool = False):
        """Delete the output of the workflow.

        Arguments
        ---------
        only_temp: bool -- Whether to only delete temporary output.
        dryrun: bool -- Whether to only dry-run the deletion.
        """
        self.workflow_api._workflow.delete_output(only_temp=only_temp, dryrun=dryrun)

    def export_to_cwl(self, path: Path):
        """Export the workflow to CWL.

        Arguments
        ---------
        path: Path -- The path to the CWL file.
        """
        self.workflow_api._workflow.export_to_cwl(path=path)


def _get_executor_plugin_registry():
    from snakemake.executors import local as local_executor
    from snakemake.executors import dryrun as dryrun_executor
    from snakemake.executors import touch as touch_executor

    registry = ExecutorPluginRegistry()
    registry.register_plugin("local", local_executor)
    registry.register_plugin("dryrun", dryrun_executor)
    registry.register_plugin("touch", touch_executor)

    return registry
