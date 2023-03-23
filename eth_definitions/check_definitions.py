from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import click

from .common import ChangeResolutionStrategy, hash_dict_on_keys

if TYPE_CHECKING:
    from .common import DEFINITION_TYPE

MAIN_KEYS = ("chain_id", "address")


def check_definitions_list(
    old_defs: list["DEFINITION_TYPE"],
    new_defs: list["DEFINITION_TYPE"],
    change_strategy: ChangeResolutionStrategy,
    top100_coingecko_ids: list[str] | None = None,
) -> None:
    # store already processed definitions
    deleted_definitions: list["DEFINITION_TYPE"] = []
    modified_definitions: list[tuple["DEFINITION_TYPE", "DEFINITION_TYPE"]] = []

    def key(definition: "DEFINITION_TYPE") -> tuple[Any, ...]:
        return tuple(definition.get(k, None) for k in MAIN_KEYS)

    def datahash(definition: "DEFINITION_TYPE") -> bytes:
        return hash_dict_on_keys(definition, exclude_keys=("deleted",))

    # dict of new definitions based on the primary keys
    defs_index = {key(nd): nd for nd in new_defs}
    # set of new definitions data hashes
    defs_data_hashed = {datahash(nd) for nd in new_defs}

    # mark all modified or deleted definitions
    for old_def in old_defs:
        old_key = key(old_def)
        if datahash(old_def) in defs_data_hashed:
            # definition was not modified
            continue
        elif old_key in defs_index:
            # definition was modified
            modified_definitions.append((old_def, defs_index[old_key]))
        else:
            # definition was deleted
            deleted_definitions.append(old_def)

    def any_in_top_100(*definitions: "DEFINITION_TYPE") -> bool:
        if top100_coingecko_ids is None:
            return True
        return any(d.get("coingecko_id") in top100_coingecko_ids for d in definitions)

    # Modified
    for old_def, new_def in modified_definitions:
        print_change = any_in_top_100(old_def, new_def)
        symbol_or_decimals_changed = False

        # Name is allowed to change, but we want to be warned
        if old_def.get("name") != new_def.get("name"):
            logging.warning("\nWARNING: Name change in this definition!")
            print_change = True

        # if the change contains symbol/decimals change "force_changes" or "interactive"
        # ChangeResolutionStrategy must be used to be able to accept this change
        if any(
            old_def.get(key) != new_def.get(key) for key in ("shortcut", "decimals")
        ):
            logging.error(
                "\nERROR: Symbol/decimals change in this definition! "
                "Use either `force_changes` or `interactive` to approve it."
            )
            print_change = True
            symbol_or_decimals_changed = True

        user_wants_to_revert = False
        if print_change:
            prompt_user = change_strategy == ChangeResolutionStrategy.PROMPT_USER
            user_wants_change = _print_definition_change(
                def_type="TOKEN" if "address" in old_def else "NETWORK",
                old=old_def,
                new=new_def,
                prompt=prompt_user,
            )
            if prompt_user and user_wants_change is False:
                user_wants_to_revert = True

        # Reject the change completely if symbol/decimals changed and the user
        # does not want to change it.
        if symbol_or_decimals_changed and (
            change_strategy == ChangeResolutionStrategy.REJECT_ALL_CHANGES
            or user_wants_to_revert
        ):
            logging.info("Definition change rejected.")
            new_def.update(old_def)

    # Deleted
    for definition in deleted_definitions:
        # mark definition as deleted
        new_definition = deepcopy(definition)
        new_definition["deleted"] = True
        new_defs.append(new_definition)


def _print_definition_change(
    def_type: str,
    old: "DEFINITION_TYPE",
    new: "DEFINITION_TYPE",
    prompt: bool = False,
) -> bool | None:
    """Print changes made between definitions and ask for prompt if requested.
    Returns the prompt result if prompted otherwise None."""
    print(f"== {def_type} MODIFIED ==")
    print("OLD:")
    print(json.dumps(old, sort_keys=True))
    print("NEW:")
    print(json.dumps(new, sort_keys=True))

    if prompt:
        return click.confirm("Confirm change:", default=False)
    else:
        return None
