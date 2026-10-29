from unittest import mock

import pytest
from botocore.exceptions import ClientError

from ecsctrl.boto_client import BotoClient
from ecsctrl.dump.secrets import SecretFetchError, dump_secrets


def make_ssm(names, failing=()):
    client = mock.Mock()
    client.describe_parameters.return_value = {
        "Parameters": [{"Name": n} for n in names]
    }

    def get_parameter(Name, WithDecryption):
        if Name in failing:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                "GetParameter",
            )
        return {"Parameter": {"Value": "v", "Type": "String"}}

    client.get_parameter.side_effect = get_parameter
    with mock.patch("boto3.client", return_value=client):
        return BotoClient("ssm"), client


def names_of(parameters):
    return [p.name for p in parameters]


def test_start_from_none_returns_everything():
    ssm, _ = make_ssm(["/a", "/b"])
    assert names_of(dump_secrets(ssm)) == ["/a", "/b"]


def test_start_from_is_inclusive():
    ssm, _ = make_ssm(["/a", "/b", "/c"])
    assert names_of(dump_secrets(ssm, start_from="/b")) == ["/b", "/c"]


def test_start_from_after_all_names_returns_nothing():
    ssm, client = make_ssm(["/a", "/b"])
    assert names_of(dump_secrets(ssm, start_from="/z")) == []
    client.get_parameter.assert_not_called()


def test_start_from_does_not_fetch_skipped_names():
    ssm, client = make_ssm(["/a", "/b"])
    list(dump_secrets(ssm, start_from="/b"))
    assert [c.kwargs["Name"] for c in client.get_parameter.call_args_list] == ["/b"]


def test_start_from_is_case_sensitive():
    ssm, _ = make_ssm(["/B", "/a"])
    assert names_of(dump_secrets(ssm, start_from="/a")) == ["/a"]


def test_fetch_error_raises_secret_fetch_error_without_handler():
    ssm, _ = make_ssm(["/a", "/b"], failing=["/a"])
    with pytest.raises(SecretFetchError) as exc_info:
        list(dump_secrets(ssm))
    assert exc_info.value.name == "/a"
    assert isinstance(exc_info.value.cause, ClientError)


def test_fetch_error_calls_handler_and_continues():
    ssm, _ = make_ssm(["/a", "/b"], failing=["/a"])
    errors = []
    result = names_of(dump_secrets(ssm, on_error=errors.append))
    assert result == ["/b"]
    assert [e.name for e in errors] == ["/a"]


def test_unrelated_exceptions_are_not_swallowed():
    ssm, client = make_ssm(["/a"])
    client.get_parameter.side_effect = ValueError("bug")
    with pytest.raises(ValueError):
        list(dump_secrets(ssm, on_error=lambda e: None))
