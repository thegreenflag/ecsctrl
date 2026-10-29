import os
import re
from email.policy import default

import click
from botocore.exceptions import BotoCoreError, ClientError

from ecsctrl.loader import VarsLoader

from .boto_client import BotoClient
from .dump import generate_var_lut
from .dump.secrets import SecretFetchError, dump_secrets, render_dumped_secrets
from .service_updater import ServiceUpdater, TaskDefinitionServiceUpdater, WaitForUpdate
from .yaml_converter import (
    JOB_DEFINITION,
    SECRETS,
    SERVICE,
    TASK_DEFINITION,
    yaml_file_to_dict,
)


def check_var(ctx, param, value):
    for v in value:
        if not re.match("^[^=]+=.*$", v):
            raise click.BadParameter(
                f"'{v}'. Variable has to be in format variable=value"
            )
    return value


# fmt: off
@click.group()
@click.option("--dry-run", is_flag=True, default=False, help="Do not call actual AWS API")
@click.pass_context
# fmt: on
def cli(ctx, dry_run):
    ctx.ensure_object(dict)
    ctx.obj["dry_run"] = dry_run
    ctx.obj["boto_client"] = BotoClient("ecs", dry_run=dry_run)


@cli.group(name="task-definition")
@click.pass_context
def task_definition(ctx):
    """Task definition management."""


def common_options(fn):
    # fmt: off
    fn = click.option("--env-file", "-e", multiple=True, type=str, help="Path to env-style file with variables")(fn)
    fn = click.option("--json-file", "-j", multiple=True, type=str, help="Path to json file with variable")(fn)
    fn = click.option("--var", "-v", multiple=True, type=str, callback=check_var, help="Single variable in format name=value")(fn)
    fn = click.option("--sys-env/--no-sys-env", is_flag=True, default=False, help="Uses system env as a source for template variables")(fn)
    # fmt: on
    return fn


def wait_options(wait_for, many=False):
    def wrapper(fn):
        s = "s" if many else ""
        # fmt: off
        fn = click.option("--wait", "-w", is_flag=True, help=f"Waits for service{s} to finish {wait_for}")(fn)
        fn = click.option("--wait-timeout", default=600, type=int, help=f"Custom timeout in seconds (defaults to 600s)")(fn)
        # fmt: on
        return fn

    return wrapper


# fmt: off
@task_definition.command()
@click.argument("spec-file", type=str)
@common_options
@click.option("--update-services-in-cluster", "-c", multiple=True, type=str, help="Updates all services deployed with this task in a particular cluster")
@wait_options(wait_for="update", many=True)
@click.pass_context
# fmt: on
def register(
    ctx,
    spec_file,
    env_file,
    json_file,
    var,
    sys_env,
    update_services_in_cluster,
    wait,
    wait_timeout,
):
    """Register task definition."""

    vars = VarsLoader(env_file, var, json_file, sys_env).load()
    spec = yaml_file_to_dict(spec_file, vars, TASK_DEFINITION)
    task_family = spec.get("family", "N/A")
    click.echo(f"🗂 Registering task definition {task_family}.")
    response = ctx.obj["boto_client"].call("register_task_definition", **spec)
    task_definition_arn = response["taskDefinition"]["taskDefinitionArn"]
    click.echo(f"\t✅ done, task definition arn: {task_definition_arn}.")

    if update_services_in_cluster and not ctx.obj["dry_run"]:
        updated_services = {}

        for cluster_name in update_services_in_cluster:
            updater = TaskDefinitionServiceUpdater(
                ctx.obj["boto_client"], task_definition_arn, cluster_name
            )
            updated_services_in_cluster = updater.update()
            updated_services[cluster_name] = updated_services_in_cluster

        if wait:
            waiter = WaitForUpdate(ctx.obj["boto_client"], updated_services)
            waiter.timeout = wait_timeout
            waiter.wait_for_all()


@cli.group(name="batch-job-definition")
@click.pass_context
def batch_job_definition(ctx):
    """Batch job definition management."""


# fmt: off
@batch_job_definition.command()
@click.argument("spec-file", type=str)
@common_options
@click.pass_context
# fmt: on
def register(
    ctx,
    spec_file,
    env_file,
    json_file,
    var,
    sys_env,
):
    """Register AWS Batch job definition."""

    vars = VarsLoader(env_file, var, json_file, sys_env).load()
    spec = yaml_file_to_dict(spec_file, vars, JOB_DEFINITION)
    job_definition_name = spec.get("jobDefinitionName", "N/A")
    click.echo(f"🗂 Registering batch job definition {job_definition_name}.")
    client = BotoClient("batch", dry_run=ctx.obj["boto_client"].dry_run)
    response = client.call("register_job_definition", **spec)
    job_definition_arn = response["jobDefinitionArn"]
    click.echo(f"\t✅ done, job definition arn: {job_definition_arn}.")


@cli.group(name="service")
@click.pass_context
def service(ctx):
    """Service management."""


@service.command()
@click.argument("spec-file", type=str)
@common_options
@wait_options(wait_for="creation")
@click.pass_context
def create(
    ctx,
    spec_file,
    env_file,
    json_file,
    var,
    sys_env,
    wait,
    wait_timeout,
):
    """Create a new service."""

    vars = VarsLoader(env_file, var, json_file, sys_env).load()
    spec = yaml_file_to_dict(spec_file, vars, SERVICE)
    service_name = spec.get("serviceName")
    cluster_name = spec.get("cluster")
    click.echo(f"🏸 Creating service {service_name}.")
    response = ctx.obj["boto_client"].call("create_service", **spec)
    service_arn = response["service"]["serviceArn"]
    click.echo("\t✅ done.")

    if wait:
        waiter = WaitForUpdate(
            ctx.obj["boto_client"],
            {cluster_name: [(service_arn, service_name)]},
        )
        waiter.timeout = wait_timeout
        waiter.wait_for_all()


@service.command()
@click.argument("spec-file", type=str)
@common_options
@wait_options(wait_for="update")
@click.pass_context
def update(
    ctx,
    spec_file,
    env_file,
    json_file,
    var,
    sys_env,
    wait,
    wait_timeout,
):
    """Update an existing service."""

    vars = VarsLoader(env_file, var, json_file, sys_env).load()
    spec = yaml_file_to_dict(spec_file, vars, SERVICE)
    service_name = spec.get("serviceName")
    cluster_name = spec.get("cluster")
    click.echo(f"🏸 Updating service {service_name}.")
    updater = ServiceUpdater()
    spec = updater.make_update_payload(spec)
    response = ctx.obj["boto_client"].call("update_service", **spec)
    service_arn = response["service"]["serviceArn"]
    click.echo("\t✅ done.")

    if wait:
        waiter = WaitForUpdate(
            ctx.obj["boto_client"],
            {cluster_name: [(service_arn, service_name)]},
        )
        waiter.timeout = wait_timeout
        waiter.wait_for_all()


@service.command("create-or-update")
@click.argument("spec-file", type=str)
@common_options
@wait_options(wait_for="update")
@click.pass_context
def create_or_update(
    ctx,
    spec_file,
    env_file,
    json_file,
    var,
    sys_env,
    wait,
    wait_timeout,
):
    """Check if service exists and update it or create a new one."""

    vars = VarsLoader(env_file, var, json_file, sys_env).load()
    spec = yaml_file_to_dict(spec_file, vars, SERVICE)
    service_name = spec.get("serviceName")
    cluster_name = spec.get("cluster")

    response = ctx.obj["boto_client"].call(
        "describe_services",
        cluster=spec["cluster"],
        services=[service_name],
    )
    service_exists = len(response["services"]) > 0

    if service_exists:
        click.echo(f"🏸 Updating service {service_name}.")
        updater = ServiceUpdater()
        spec = updater.make_update_payload(spec)
        response = ctx.obj["boto_client"].call("update_service", **spec)
        click.echo("\t✅ done.")
    else:
        click.echo(f"🏸 Creating service {service_name}.")
        response = ctx.obj["boto_client"].call("create_service", **spec)
        click.echo("\t✅ done.")
    service_arn = response["service"]["serviceArn"]

    if wait:
        waiter = WaitForUpdate(
            ctx.obj["boto_client"],
            {cluster_name: [(service_arn, service_name)]},
        )
        waiter.timeout = wait_timeout
        waiter.wait_for_all()


@cli.group(name="secrets")
@click.pass_context
def secrets(ctx):
    """Secrets management."""


@secrets.command()
@click.argument("spec-file", type=str)
@common_options
@click.pass_context
def store(
    ctx,
    spec_file,
    env_file,
    json_file,
    var,
    sys_env,
):
    """Store secrets is Parameter Store."""
    vars = VarsLoader(env_file, var, json_file, sys_env).load()
    spec = yaml_file_to_dict(spec_file, vars, SECRETS)
    ssm = BotoClient("ssm", dry_run=ctx.obj["boto_client"].dry_run)

    for secret_name, value in spec.items():
        common_ssm_params = {
            "Name": secret_name,
            "Overwrite": True,
            "Tier": "Intelligent-Tiering",
        }

        if isinstance(value, str):
            ssm_params = {
                **common_ssm_params,
                "Value": value,
                "Type": "SecureString",
            }
        else:
            ssm_params = {
                **common_ssm_params,
                "Value": value["Value"],
                "Type": value["Type"],
            }
            if value.get("Description", None):
                ssm_params["Description"] = value["Description"]

        click.echo(f"🔑 Storing secret {secret_name}.")
        response = ssm.call("put_parameter", **ssm_params)
        click.echo(f"\t✅ done, parameter version: {response['Version']}")


@secrets.command()
@click.argument("spec-file", type=str)
@click.option(
    "--filter",
    type=str,
    default=None,
    help="Export only secrets matching given regexp pattern",
)
@click.option(
    "--start-from",
    type=str,
    default=None,
    help="Export only secrets whose names are alphabetically equal to or after given value",
)
@click.option(
    "--simple",
    is_flag=True,
    default=False,
    help="Write every secret as `key: value`, omitting its type and description",
)
@click.option(
    "--skip-errors",
    is_flag=True,
    default=False,
    help="Skip secrets that fail to be fetched (reported on stderr) instead of aborting",
)
@common_options
@click.pass_context
def dump(
    ctx,
    spec_file,
    filter,
    start_from,
    simple,
    skip_errors,
    env_file,
    json_file,
    var,
    sys_env,
):
    """Dump secrets from Parameter Store."""
    vars = VarsLoader(env_file, var, json_file, sys_env).load()
    var_lut = generate_var_lut(vars)
    ssm = BotoClient("ssm", dry_run=ctx.obj["boto_client"].dry_run)

    def on_error(error):
        click.echo(f"⚠️ Skipping {error.name}: {error.cause}", err=True)

    secrets = dump_secrets(
        ssm, filter, on_error if skip_errors else None, start_from=start_from
    )
    try:
        render_dumped_secrets(click, secrets, var_lut, spec_file, simple)
    except SecretFetchError as e:
        raise click.ClickException(
            f"{e.name}: {e.cause}\n"
            f"File {spec_file} was not written. "
            "Use --skip-errors to skip secrets that fail to be fetched."
        )
    except (BotoCoreError, ClientError) as e:
        raise click.ClickException(f"Failed to list secrets: {e}")


@service.command()
@click.argument("task-definition-spec-file", type=str)
@click.argument("service-spec-file", type=str)
@common_options
@wait_options(wait_for="update")
@click.pass_context
def deploy(
    ctx,
    task_definition_spec_file,
    service_spec_file,
    env_file,
    json_file,
    var,
    sys_env,
    wait,
    wait_timeout,
):
    """All-in-one - register task definition and create or update service."""

    vars = VarsLoader(env_file, var, json_file, sys_env).load()
    task_definition_spec = yaml_file_to_dict(
        task_definition_spec_file, vars, TASK_DEFINITION
    )
    task_family = task_definition_spec.get("family", "N/A")
    click.echo(f"🗂 Registering task definition {task_family}.")
    response = ctx.obj["boto_client"].call(
        "register_task_definition", **task_definition_spec
    )
    task_definition_arn = response["taskDefinition"]["taskDefinitionArn"]
    click.echo(f"\t✅ done, task definition arn: {task_definition_arn}.")

    service_spec = yaml_file_to_dict(service_spec_file, vars, SERVICE)
    service_name = service_spec.get("serviceName")
    cluster_name = service_spec.get("cluster")
    service_spec["taskDefinition"] = task_definition_arn

    response = ctx.obj["boto_client"].call(
        "describe_services",
        cluster=service_spec["cluster"],
        services=[service_name],
    )
    existing_services = list(
        filter(lambda s: s["status"] != "INACTIVE", response["services"])
    )
    service_exists = len(existing_services) > 0

    if service_exists:
        click.echo(f"🏸 Updating service {service_name}.")
        updater = ServiceUpdater()
        service_spec = updater.make_update_payload(service_spec)
        response = ctx.obj["boto_client"].call("update_service", **service_spec)
        click.echo("\t✅ done.")
    else:
        click.echo(f"🏸 Creating service {service_name}.")
        response = ctx.obj["boto_client"].call("create_service", **service_spec)
        click.echo("\t✅ done.")
    service_arn = response["service"]["serviceArn"]

    if wait:
        waiter = WaitForUpdate(
            ctx.obj["boto_client"],
            {cluster_name: [(service_arn, service_name)]},
        )
        waiter.timeout = wait_timeout
        waiter.wait_for_all()
