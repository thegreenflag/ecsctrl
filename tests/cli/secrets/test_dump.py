from unittest import mock

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from ecsctrl.cli import cli


def client_error(operation, code="AccessDeniedException"):
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, operation)


def make_ssm_mock(failing=()):
    ssm = mock.Mock()
    ssm.describe_parameters.return_value = {
        "Parameters": [{"Name": "/a"}, {"Name": "/b"}, {"Name": "/c"}]
    }

    def get_parameter(Name, WithDecryption):
        if Name in failing:
            raise client_error("GetParameter")
        return {"Parameter": {"Value": f"value{Name}", "Type": "SecureString"}}

    ssm.get_parameter.side_effect = get_parameter
    return ssm


def invoke(tmp_path, *extra_args):
    target = tmp_path / "secrets.yaml"
    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:  # click >= 8.2 always separates stderr
        runner = CliRunner()
    result = runner.invoke(cli, ["secrets", "dump", *extra_args, str(target)])
    return result, target


@mock.patch("boto3.client")
def test_dump_all(boto_mock, tmp_path):
    boto_mock.return_value = make_ssm_mock()

    result, target = invoke(tmp_path)

    assert result.exit_code == 0
    assert "/a: value/a" in target.read_text()
    assert "/b: value/b" in target.read_text()
    assert "/c: value/c" in target.read_text()


@mock.patch("boto3.client")
def test_dump_fails_fast_by_default(boto_mock, tmp_path):
    boto_mock.return_value = make_ssm_mock(failing=["/b"])

    result, target = invoke(tmp_path)

    assert result.exit_code == 1
    assert "/b" in result.stderr
    assert "AccessDeniedException" in result.stderr
    assert "Traceback" not in result.stderr
    assert not target.exists()


@mock.patch("boto3.client")
def test_dump_skip_errors(boto_mock, tmp_path):
    boto_mock.return_value = make_ssm_mock(failing=["/b"])

    result, target = invoke(tmp_path, "--skip-errors")

    content = target.read_text()
    assert result.exit_code == 0
    assert "/a: value/a" in content
    assert "/b" not in content
    assert "/c: value/c" in content
    assert "/b" in result.stderr
    assert "AccessDeniedException" in result.stderr
    assert "/b" not in result.stdout


@mock.patch("boto3.client")
def test_dump_list_failure_is_fatal_even_with_skip_errors(boto_mock, tmp_path):
    ssm = make_ssm_mock()
    ssm.describe_parameters.side_effect = client_error("DescribeParameters")
    boto_mock.return_value = ssm

    result, target = invoke(tmp_path, "--skip-errors")

    assert result.exit_code == 1
    assert "AccessDeniedException" in result.stderr
    assert "Traceback" not in result.stderr
    assert not target.exists()


@mock.patch("boto3.client")
def test_dump_start_from(boto_mock, tmp_path):
    ssm = make_ssm_mock()
    boto_mock.return_value = ssm

    result, target = invoke(tmp_path, "--start-from", "/b")

    content = target.read_text()
    assert result.exit_code == 0
    assert "/a" not in content
    assert "/b: value/b" in content
    assert "/c: value/c" in content
    fetched = [c.kwargs["Name"] for c in ssm.get_parameter.call_args_list]
    assert fetched == ["/b", "/c"]


@mock.patch("boto3.client")
def test_dump_start_from_between_names(boto_mock, tmp_path):
    ssm = make_ssm_mock()
    boto_mock.return_value = ssm

    result, target = invoke(tmp_path, "--start-from", "/aa")

    content = target.read_text()
    assert result.exit_code == 0
    assert "/a:" not in content
    assert "/b: value/b" in content
    assert "/c: value/c" in content


@mock.patch("boto3.client")
def test_dump_start_from_with_filter(boto_mock, tmp_path):
    boto_mock.return_value = make_ssm_mock()

    result, target = invoke(tmp_path, "--start-from", "/b", "--filter", "/c")

    content = target.read_text()
    assert result.exit_code == 0
    assert "/b" not in content
    assert "/c: value/c" in content


@mock.patch("boto3.client")
def test_dump_start_from_with_skip_errors(boto_mock, tmp_path):
    ssm = make_ssm_mock(failing=["/a", "/b"])
    boto_mock.return_value = ssm

    result, target = invoke(tmp_path, "--start-from", "/b", "--skip-errors")

    content = target.read_text()
    assert result.exit_code == 0
    assert "/c: value/c" in content
    assert "/a" not in result.stderr
    assert "/b" in result.stderr


@mock.patch("boto3.client")
def test_dump_start_from_with_unsorted_listing(boto_mock, tmp_path):
    ssm = make_ssm_mock()
    ssm.describe_parameters.return_value = {
        "Parameters": [{"Name": "/c"}, {"Name": "/a"}, {"Name": "/b"}]
    }
    boto_mock.return_value = ssm

    result, target = invoke(tmp_path, "--start-from", "/b")

    content = target.read_text()
    assert result.exit_code == 0
    assert "/a" not in content
    assert "/b: value/b" in content
    assert "/c: value/c" in content


@mock.patch("boto3.client")
def test_dump_start_from_across_pages(boto_mock, tmp_path):
    ssm = make_ssm_mock()
    ssm.describe_parameters.side_effect = [
        {"Parameters": [{"Name": "/a"}], "NextToken": "t"},
        {"Parameters": [{"Name": "/b"}, {"Name": "/c"}]},
    ]
    boto_mock.return_value = ssm

    result, target = invoke(tmp_path, "--start-from", "/b")

    content = target.read_text()
    assert result.exit_code == 0
    assert "/a" not in content
    assert "/b: value/b" in content
    assert "/c: value/c" in content
    assert ssm.describe_parameters.call_count == 2


def make_mixed_ssm_mock():
    ssm = mock.Mock()
    ssm.describe_parameters.return_value = {
        "Parameters": [
            {"Name": "/plain"},
            {"Name": "/described", "Description": "some description"},
            {"Name": "/multiline"},
        ]
    }
    values = {
        "/plain": {"Value": "p", "Type": "String"},
        "/described": {"Value": "d", "Type": "SecureString"},
        "/multiline": {"Value": "l1\nl2", "Type": "StringList"},
    }
    ssm.get_parameter.side_effect = lambda Name, WithDecryption: {
        "Parameter": values[Name]
    }
    return ssm


@mock.patch("boto3.client")
def test_dump_default_keeps_complex_format(boto_mock, tmp_path):
    boto_mock.return_value = make_mixed_ssm_mock()

    result, target = invoke(tmp_path)

    content = target.read_text()
    assert result.exit_code == 0
    assert "/plain:\n  Type: String\n  Value: p\n" in content
    assert "Description: some description" in content


@mock.patch("boto3.client")
def test_dump_simple(boto_mock, tmp_path):
    boto_mock.return_value = make_mixed_ssm_mock()

    result, target = invoke(tmp_path, "--simple")

    content = target.read_text()
    assert result.exit_code == 0
    assert "/plain: p\n" in content
    assert "/described: d\n" in content
    assert "/multiline: |-\n  l1\n  l2\n" in content
    assert "Type:" not in content
    assert "Value:" not in content
    assert "Description:" not in content


@mock.patch("boto3.client")
def test_dump_simple_output_can_be_loaded_back(boto_mock, tmp_path):
    from ecsctrl.yaml_converter import SECRETS, yaml_to_dict

    boto_mock.return_value = make_mixed_ssm_mock()

    result, target = invoke(tmp_path, "--simple")

    assert yaml_to_dict(target.read_text(), SECRETS) == {
        "/plain": "p",
        "/described": "d",
        "/multiline": "l1\nl2",
    }


@mock.patch("boto3.client")
def test_dump_failure_does_not_report_secrets_as_dumped(boto_mock, tmp_path):
    boto_mock.return_value = make_ssm_mock(failing=["/b"])

    result, target = invoke(tmp_path)

    assert result.exit_code == 1
    assert not target.exists()
    assert "Dumped" not in result.stdout


@mock.patch("boto3.client")
def test_dump_failure_hints_at_skip_errors(boto_mock, tmp_path):
    boto_mock.return_value = make_ssm_mock(failing=["/b"])

    result, target = invoke(tmp_path)

    assert "--skip-errors" in result.stderr
    assert "not written" in result.stderr


@mock.patch("boto3.client")
def test_dump_reports_secrets_after_writing_file(boto_mock, tmp_path):
    boto_mock.return_value = make_ssm_mock()

    result, target = invoke(tmp_path)

    assert result.exit_code == 0
    assert "Dumped secret /a as /a." in result.stdout
    assert "Dumped secret /c as /c." in result.stdout
