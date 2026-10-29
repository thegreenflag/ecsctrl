import re
from botocore.exceptions import BotoCoreError, ClientError
from . import substitute_with_expressions
from dataclasses import dataclass
from typing import Generator, Optional, Sequence


@dataclass
class Parameter:
    name: str
    value: str
    type: str
    description: Optional[str]


def list_secrets(ssm):
    should_fetch = True
    next_token = None

    while should_fetch:
        kwargs = {}
        if next_token:
            kwargs["NextToken"] = next_token
        response = ssm.call("describe_parameters", **kwargs)
        next_token = response.get("NextToken")
        should_fetch = bool(next_token)

        for parameter in response["Parameters"]:
            yield parameter


class SecretFetchError(Exception):
    def __init__(self, name, cause):
        super().__init__(f"Failed to fetch {name}: {cause}")
        self.name = name
        self.cause = cause


def dump_secrets(
    ssm, filter=None, on_error=None, start_from=None
) -> Generator[Parameter, None, None]:
    for parameter in list_secrets(ssm):
        parameter_name = parameter["Name"]
        if start_from is not None and parameter_name < start_from:
            continue
        if filter is None or re.match(filter, parameter_name):
            try:
                response = ssm.call(
                    "get_parameter",
                    Name=parameter_name,
                    WithDecryption=True,
                )
            except (BotoCoreError, ClientError) as e:
                error = SecretFetchError(parameter_name, e)
                if on_error is None:
                    raise error from e
                on_error(error)
                continue

            yield Parameter(
                name=parameter_name,
                value=response["Parameter"]["Value"],
                type=response["Parameter"]["Type"],
                description=parameter.get("Description", None),
            )


def render_dumped_secrets(
    click, secrets: Sequence[Parameter], vars_lut, target_file, simple=False
):
    f = None
    try:
        for parameter in secrets:
            key, secret_text = rended_single_secret(parameter, vars_lut, simple)
            if f is None:
                f = open(target_file, "w")
            f.write(f"# Dumped from `{parameter.name}`:\n{secret_text}\n")
            f.flush()
            click.echo(f"🔑 Dumped secret {parameter.name} as {key}.")

        if f is None:
            open(target_file, "w").close()
    finally:
        if f is not None:
            f.close()


def rended_single_secret(parameter: Parameter, vars_lut, simple=False):
    name = parameter.name
    key = substitute_with_expressions(name, vars_lut)
    if simple or (parameter.type == "SecureString" and parameter.description is None):
        secret_text = render_simple_secret(key, parameter)
    else:
        secret_text = render_complex_secret(key, parameter)
    return key, secret_text


def render_simple_secret(key, parameter):
    value = render_value(parameter.value, 1)
    secret_text = f"{key}: {value}\n"
    return secret_text


def render_complex_secret(key, parameter):
    value = render_value(parameter.value, 2)
    secret_text = f"{key}:\n"
    secret_text += f"  Type: {parameter.type}\n"
    secret_text += f"  Value: {value}\n"
    if parameter.description:
        description = render_value(parameter.description, 2)
        secret_text += f"  Description: {description}\n"
    return secret_text


def render_value(value: str, indent: int) -> str:
    if "\n" in value:
        last_nl = "-"
        if value.endswith("\n"):
            last_nl = ""
        result_lines = [f"|{last_nl}"]
        for line in value.split("\n"):
            result_lines.append(("  " * indent) + line)
        return "\n".join(result_lines)

    return value
