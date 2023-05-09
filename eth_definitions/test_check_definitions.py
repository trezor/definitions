from copy import deepcopy
from io import StringIO
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from .check_definitions import check_definitions_list
from .common import ChangeResolutionStrategy
from .test_data import networks, tokens

if TYPE_CHECKING:
    from common import DEFINITION_TYPE


parametrized = pytest.mark.parametrize(
    "old, new",
    (
        (networks, networks),
        (tokens, tokens),
    ),
)


@parametrized
def test_check_definitions_list_no_change(
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=False,
                force_accept=False,
            ),
            show_all=True,
        )

        assert len(caplog.records) == 0
        assert mock_stdout.getvalue() == ""

    # Nothing changed
    assert old_defs == old
    assert new_defs == new


@parametrized
def test_check_definitions_list_added_new(
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    # Simulating addition
    old = old[:-1]

    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=False,
                force_accept=False,
            ),
            show_all=True,
        )

        assert len(caplog.records) == 0
        assert mock_stdout.getvalue() == ""

    # Nothing changed
    assert old_defs == old
    assert new_defs == new


@parametrized
def test_check_definitions_list_deleted(
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    # Simulating deletion
    new = new[:-1]

    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=False,
                force_accept=False,
            ),
            show_all=True,
        )

        assert len(caplog.records) == 0
        assert mock_stdout.getvalue() == ""

    # One definition marked as deleted and included in new_defs
    assert old_defs == old
    assert len(new_defs) == len(new) + 1
    assert new_defs[:-1] == new
    assert "deleted" in new_defs[-1]


@parametrized
def test_check_definitions_list_deleted_in_old(
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    # Simulating deletion
    old = old[:1]
    old[0]["deleted"] = True
    old_defs = deepcopy(old)
    new = []
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=False,
                force_accept=False,
            ),
            show_all=True,
        )

        assert len(caplog.records) == 0
        assert mock_stdout.getvalue() == ""

    # One definition marked as deleted and included in new_defs
    assert old_defs == old
    assert new_defs == old


@parametrized
def test_check_definitions_list_resurrected(
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    # Add deleted mark to old
    old[-1]["deleted"] = True
    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=False,
                force_accept=False,
            ),
            show_all=True,
        )

        assert len(caplog.records) == 0
        assert mock_stdout.getvalue() == ""

    # Nothing changed
    assert old_defs == old
    assert new_defs == new


@parametrized
def test_check_definitions_list_modified_name(
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    old_name = "DIFFERENT NAME"
    new_name = new[-1]["name"]
    old[-1]["name"] = old_name

    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=False,
                force_accept=False,
            ),
            show_all=True,
        )

        assert len(caplog.records) == 1
        log = caplog.records[0]
        assert log.levelname == "WARNING"
        assert "Name change in this definition!" in log.msg

        assert mock_stdout.getvalue().count("MODIFIED ==") == 1
        assert mock_stdout.getvalue().count("OLD:") == 1
        assert mock_stdout.getvalue().count("NEW:") == 1
        assert mock_stdout.getvalue().count(f'"name": "{old_name}"') == 1
        assert mock_stdout.getvalue().count(f'"name": "{new_name}"') == 1

    # Nothing changed
    assert old_defs == old
    assert new_defs == new


@parametrized
def test_check_definitions_list_modified_shortcut_no_force(
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    old_shortcut = "ABC"
    new_shortcut = new[-1]["shortcut"]
    old[-1]["shortcut"] = old_shortcut

    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=False,
                force_accept=False,
            ),
            show_all=True,
        )

        assert len(caplog.records) == 2
        error_log = caplog.records[0]
        assert error_log.levelname == "ERROR"
        assert "Symbol/decimals change in this definition!" in error_log.msg
        info_log = caplog.records[1]
        assert info_log.levelname == "INFO"
        assert "Definition change rejected" in info_log.msg

        assert mock_stdout.getvalue().count("MODIFIED ==") == 1
        assert mock_stdout.getvalue().count("OLD:") == 1
        assert mock_stdout.getvalue().count("NEW:") == 1
        assert mock_stdout.getvalue().count(f'"shortcut": "{old_shortcut}"') == 1
        assert mock_stdout.getvalue().count(f'"shortcut": "{new_shortcut}"') == 1

    # Shortcut change was reverted
    assert old_defs == old
    assert new_defs != new
    assert new_defs == old


@parametrized
def test_check_definitions_list_modified_shortcut_force(
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    old_shortcut = "ABC"
    new_shortcut = new[-1]["shortcut"]
    old[-1]["shortcut"] = old_shortcut

    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=False,
                force_accept=True,
            ),
            show_all=True,
        )

        assert len(caplog.records) == 1
        log = caplog.records[0]
        assert log.levelname == "ERROR"
        assert "Symbol/decimals change in this definition!" in log.msg

        assert mock_stdout.getvalue().count("MODIFIED ==") == 1
        assert mock_stdout.getvalue().count("OLD:") == 1
        assert mock_stdout.getvalue().count("NEW:") == 1
        assert mock_stdout.getvalue().count(f'"shortcut": "{old_shortcut}"') == 1
        assert mock_stdout.getvalue().count(f'"shortcut": "{new_shortcut}"') == 1

    # Nothing changed, shortcut change was kept
    assert old_defs == old
    assert new_defs == new


@parametrized
@mock.patch("click.confirm", return_value=True)
def test_check_definitions_list_modified_shortcut_interact_accept(
    mock_confirm: mock.MagicMock,
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    old_shortcut = "ABC"
    new_shortcut = new[-1]["shortcut"]
    old[-1]["shortcut"] = old_shortcut

    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=True,
                force_accept=False,
            ),
            show_all=True,
        )

        assert mock_confirm.call_count == 1

        assert len(caplog.records) == 1
        log = caplog.records[0]
        assert log.levelname == "ERROR"
        assert "Symbol/decimals change in this definition!" in log.msg

        assert mock_stdout.getvalue().count("MODIFIED ==") == 1
        assert mock_stdout.getvalue().count("OLD:") == 1
        assert mock_stdout.getvalue().count("NEW:") == 1
        assert mock_stdout.getvalue().count(f'"shortcut": "{old_shortcut}"') == 1
        assert mock_stdout.getvalue().count(f'"shortcut": "{new_shortcut}"') == 1

    # Nothing changed, shortcut change was kept
    assert old_defs == old
    assert new_defs == new


@parametrized
@mock.patch("click.confirm", return_value=False)
def test_check_definitions_list_modified_shortcut_interact_decline(
    mock_confirm: mock.MagicMock,
    caplog: pytest.LogCaptureFixture,
    old: list["DEFINITION_TYPE"],
    new: list["DEFINITION_TYPE"],
):
    old = deepcopy(old)
    new = deepcopy(new)

    old_shortcut = "ABC"
    new_shortcut = new[-1]["shortcut"]
    old[-1]["shortcut"] = old_shortcut

    old_defs = deepcopy(old)
    new_defs = deepcopy(new)

    with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
        check_definitions_list(
            old_defs=old_defs,
            new_defs=new_defs,
            change_strategy=ChangeResolutionStrategy.from_args(
                interactive=True,
                force_accept=False,
            ),
            show_all=True,
        )

        assert mock_confirm.call_count == 1

        assert len(caplog.records) == 2
        error_log = caplog.records[0]
        assert error_log.levelname == "ERROR"
        assert "Symbol/decimals change in this definition!" in error_log.msg
        info_log = caplog.records[1]
        assert info_log.levelname == "INFO"
        assert "Definition change rejected" in info_log.msg

        assert mock_stdout.getvalue().count("MODIFIED ==") == 1
        assert mock_stdout.getvalue().count("OLD:") == 1
        assert mock_stdout.getvalue().count("NEW:") == 1
        assert mock_stdout.getvalue().count(f'"shortcut": "{old_shortcut}"') == 1
        assert mock_stdout.getvalue().count(f'"shortcut": "{new_shortcut}"') == 1

    # Shortcut change was reverted
    assert old_defs == old
    assert new_defs != new
    assert new_defs == old
