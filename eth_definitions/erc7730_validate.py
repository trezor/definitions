"""Independent cross-check of extracted display formats against their source.

`erc7730.py` walks a raw ERC-7730 descriptor and emits the records that land in
`definitions-latest.json`. This module re-derives, *independently of that walk*,
what the records for a descriptor should look like — which signatures, how many
deployments, and for each visible field its label / formatter / token-path /
resolved index path — and asserts the emitted records match.

The point is to catch silent drops or corruptions in the extractor (e.g. a
field quietly disappearing because a type wasn't handled). So the re-derivation
deliberately does NOT reuse the extractor's field/path logic; it only shares the
low-level signature-hashing and `includes`-merge utilities from the `erc7730`
library (deterministic, not where extraction bugs hide).

`validate_file()` prints a per-file summary table and raises `ValidationError`
on any mismatch, so the download pipeline crashes loudly rather than shipping a
display format that doesn't faithfully represent its descriptor.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import click
from erc7730.common.abi import (
    compute_signature,
    parse_signature,
    signature_to_selector,
)

from .common import ERC20DisplayFormat
from .erc7730 import load_descriptor

# Same Solidity-format → proto-formatter mapping the extractor uses. Any
# descriptor field that survived into a record used one of these; an exotic
# formatter would have made the extractor skip the whole file (no records),
# so we never have to validate it.
_FORMATTER_MAP = {
    "addressName": "FORMATTER_ADDRESS_NAME",
    "amount": "FORMATTER_AMOUNT",
    "tokenAmount": "FORMATTER_TOKEN_AMOUNT",
    "unit": "FORMATTER_UNIT",
}


class ValidationError(Exception):
    """An emitted display format does not match its source descriptor."""


# =====================================================================
#                 Independent descriptor re-derivation
# =====================================================================


def _intent(display_format: dict[str, Any]) -> str:
    intent = display_format.get("intent", "")
    if isinstance(intent, dict):
        return intent.get("en") or next(iter(intent.values()), "")
    return intent or ""


def _resolve_ref(field: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    """Merge a `$.display.definitions.*` reference into a field dict.

    Field keys override the definition; `params` is deep-merged. Mirrors the
    spec's `$ref` semantics independently of the extractor's own resolver.
    """
    ref = field.get("$ref")
    if not ref:
        return field
    # Only a `$.display.definitions.<key>` ref is resolvable; anything else
    # (a bare string, a `$.metadata.*` path, …) the extractor ignores, so we
    # must too — otherwise we'd derive an expectation from a definition the
    # extractor never merged and the comparison would diverge.
    prefix = "$.display.definitions."
    ref = str(ref)
    if not ref.startswith(prefix):
        return field
    key = ref[len(prefix):]
    definition = definitions.get(key)
    if definition is None:
        return field
    merged: dict[str, Any] = {**definition}
    for k, v in field.items():
        if k == "$ref":
            continue
        if k == "params" and isinstance(v, dict) and isinstance(merged.get("params"), dict):
            merged["params"] = {**merged["params"], **v}
        else:
            merged[k] = v
    return merged


def _is_displayed(field: dict[str, Any]) -> bool:
    """A field is shown when it has a `format` and isn't `visible: never/false`."""
    if field.get("format") is None:
        return False
    return field.get("visible") not in (False, "never")


# Transaction-level container paths (`@.value` / `@.from` / `@.to`) → proto enum.
_CONTAINER_MAP = {"value": "VALUE", "from": "FROM", "to": "TO"}

_SEGMENT_RE = re.compile(r"\[(-?\d+)\]|\[\]|([^.\[\]]+)")


def _resolve_container(path_str: str) -> str | None:
    """Map a container path (`@.value` / `@.from` / `@.to`) to its proto enum.

    Returns None for anything that isn't a recognized container path (a data
    path, descriptor path, or an unknown `@.` field). An unknown `@.` field is
    deliberately *not* mapped: the extractor rejects it (so no record is
    emitted) and we never validate it.
    """
    s = path_str.strip()
    if not s.startswith("@"):
        return None
    field = s.lstrip("@").lstrip(".").strip()
    return _CONTAINER_MAP.get(field)


def _resolve_indices(path_str: str, inputs: list) -> list[int] | None:
    """Resolve an ERC-7730 *data* path to the proto index list, independently.

    Returns the list of indices (array indices may be negative, e.g. `-1` for
    the last element), or None if the path isn't a plain data path we can
    resolve (container `@.`, descriptor `$.`, whole-array iteration `.[]`, or an
    unknown name segment). A None result means "don't index-check this field";
    it is never itself treated as a mismatch.
    """
    s = path_str.strip()
    if s.startswith(("@", "$")):
        return None

    indices: list[int] = []
    current = inputs
    for m in _SEGMENT_RE.finditer(s):
        array_idx, name = m.group(1), m.group(2)
        if name is not None:
            by_name = {c.name: i for i, c in enumerate(current) if c.name}
            if name not in by_name:
                return None
            i = by_name[name]
            indices.append(i)
            current = current[i].components or []
        elif array_idx is not None:
            # `[N]` / `[-1]` — index into an array, element type unchanged.
            indices.append(int(array_idx))
        else:
            # `[]` whole-array iteration — not representable as a fixed path.
            return None
    return indices


class _ExpectedField:
    __slots__ = ("label", "formatter", "path", "container_path", "token_path", "has_token_path")

    def __init__(self, field: dict[str, Any], inputs: list):
        fmt = field["format"]
        path_str = str(field.get("path", ""))
        self.label: str = field.get("label", "")
        self.formatter: str | None = _FORMATTER_MAP.get(fmt)
        self.container_path = _resolve_container(path_str)
        self.path = _resolve_indices(path_str, inputs)
        params = field.get("params") or {}
        token_path_str = params.get("tokenPath") if fmt == "tokenAmount" else None
        self.has_token_path = bool(token_path_str)
        self.token_path = (
            _resolve_indices(str(token_path_str), inputs) if token_path_str else None
        )


class _ExpectedFormat:
    __slots__ = ("name", "selector", "intent", "fields")

    def __init__(self, sig: str, display_format: dict[str, Any], definitions: dict[str, Any]):
        parsed = parse_signature(sig)
        inputs = list(parsed.inputs or [])
        self.name = sig.split("(")[0]
        self.selector = signature_to_selector(compute_signature(parsed))
        self.intent = _intent(display_format)
        self.fields: list[_ExpectedField] = []
        for f in display_format.get("fields", []):
            if not isinstance(f, dict):
                continue
            f = _resolve_ref(f, definitions)
            if not _is_displayed(f):
                continue
            self.fields.append(_ExpectedField(f, inputs))


def _valid_deployment_count(descriptor: dict[str, Any]) -> int:
    """Count deployments the extractor would emit a record for.

    Mirrors the extractor's per-deployment validity gate: integer chainId > 0
    and a 20-byte hex address.
    """
    contract = (descriptor.get("context") or {}).get("contract") or {}
    count = 0
    for dep in contract.get("deployments") or []:
        chain_id, address = dep.get("chainId"), dep.get("address")
        if chain_id is None or not address:
            continue
        try:
            if int(chain_id) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        addr = str(address).lower()
        addr = addr[2:] if addr.startswith("0x") else addr
        if len(addr) != 40 or not re.fullmatch(r"[0-9a-f]+", addr):
            continue
        count += 1
    return count


def derive_expected(descriptor: dict[str, Any]) -> dict[str, _ExpectedFormat]:
    """Independently derive the expected records, keyed by selector."""
    display = descriptor.get("display") or {}
    formats = display.get("formats") or {}
    definitions = display.get("definitions") or {}
    expected: dict[str, _ExpectedFormat] = {}
    for sig, fmt in formats.items():
        if sig.startswith("0x") or not isinstance(fmt, dict):
            continue  # selector-only entries carry no signature to re-derive
        try:
            ef = _ExpectedFormat(sig, fmt, definitions)
        except Exception:
            continue  # unparseable signature — extractor skips it too
        expected[ef.selector] = ef
    return expected


# =====================================================================
#                              Comparison
# =====================================================================


def validate_file(
    source: str,
    descriptor: dict[str, Any],
    records: list[ERC20DisplayFormat],
    *,
    print_summary: bool = True,
) -> list[str]:
    """Cross-check `records` (the extractor output for `descriptor`).

    Prints a per-file summary table and returns the list of mismatch messages
    (empty when clean). Raises `ValidationError` on the first call site that
    wants hard failure — see `validate_file_strict`.
    """
    expected = derive_expected(descriptor)
    expected_deployments = _valid_deployment_count(descriptor)

    # Group emitted records by selector.
    by_selector: dict[str, list[ERC20DisplayFormat]] = {}
    for r in records:
        by_selector.setdefault(r["func_sig"], []).append(r)

    errors: list[str] = []
    rows: list[tuple[str, str, str, str]] = []

    emitted_selectors = set(by_selector)
    expected_selectors = set(expected)

    for sel in sorted(emitted_selectors - expected_selectors):
        errors.append(f"emitted selector {sel} not present in descriptor {source}")

    for sel in sorted(expected_selectors):
        ef = expected[sel]
        recs = by_selector.get(sel)
        if not recs:
            # A format with zero visible fields still yields records (empty
            # field list); only a genuinely absent selector is a problem, and
            # only if the descriptor's deployments were valid.
            if expected_deployments > 0:
                errors.append(
                    f"{source} {sel} ({ef.name}): expected records, none emitted"
                )
                rows.append((sel, ef.name, str(len(ef.fields)), "MISSING"))
            continue

        status = "OK"

        # Deployment cross-product: one record per valid deployment.
        n_deploy = len({(r["chain_id"], r["address"]) for r in recs})
        if n_deploy != expected_deployments:
            errors.append(
                f"{source} {sel} ({ef.name}): {n_deploy} deployments emitted, "
                f"expected {expected_deployments}"
            )
            status = "MISMATCH"

        # Every deployment of a selector must carry identical field defs; pick
        # one and compare its fields against the independent derivation.
        sample = recs[0]
        got = sample["field_definitions"]
        if len(got) != len(ef.fields):
            errors.append(
                f"{source} {sel} ({ef.name}): {len(got)} fields emitted, "
                f"expected {len(ef.fields)} visible "
                f"({[f.label for f in ef.fields]} vs {[g.get('label') for g in got]})"
            )
            status = "MISMATCH"
        else:
            for exp, g in zip(ef.fields, got):
                ctx = f"{source} {sel} ({ef.name}) field {exp.label!r}"
                if g.get("label") != exp.label:
                    errors.append(f"{ctx}: label {g.get('label')!r} != {exp.label!r}")
                    status = "MISMATCH"
                if exp.formatter and g.get("formatter") != exp.formatter:
                    errors.append(
                        f"{ctx}: formatter {g.get('formatter')!r} != {exp.formatter!r}"
                    )
                    status = "MISMATCH"
                has_tp = "token_path" in g
                if has_tp != exp.has_token_path:
                    errors.append(
                        f"{ctx}: token_path present={has_tp}, expected={exp.has_token_path}"
                    )
                    status = "MISMATCH"
                # Container path (`@.value/from/to`) → must be emitted as the
                # matching `container_path` enum inside the path envelope, not a
                # data path.
                if exp.container_path is not None:
                    got_cp = g.get("path", {}).get("container_path")
                    if got_cp != exp.container_path:
                        errors.append(
                            f"{ctx}: container_path {got_cp!r} != {exp.container_path!r}"
                        )
                        status = "MISMATCH"
                # Index paths are checked only when our independent resolver
                # could resolve the descriptor path (None = un-checkable, not a
                # mismatch). Container-path fields have no index path.
                elif exp.path is not None and g.get("path", {}).get("path") != exp.path:
                    errors.append(
                        f"{ctx}: path {g.get('path', {}).get('path')} != {exp.path}"
                    )
                    status = "MISMATCH"
                if (
                    exp.has_token_path
                    and exp.token_path is not None
                    and g.get("token_path", {}).get("path") != exp.token_path
                ):
                    errors.append(
                        f"{ctx}: token_path {g.get('token_path', {}).get('path')} "
                        f"!= {exp.token_path}"
                    )
                    status = "MISMATCH"

        if sample.get("intent") != ef.intent:
            errors.append(
                f"{source} {sel} ({ef.name}): intent {sample.get('intent')!r} "
                f"!= {ef.intent!r}"
            )
            status = "MISMATCH"

        rows.append((sel, ef.name, str(len(ef.fields)), status))

    if print_summary:
        _print_summary(source, expected, records, expected_deployments, rows)

    return errors


def validate_file_strict(
    source: str,
    descriptor: dict[str, Any],
    records: list[ERC20DisplayFormat],
    *,
    print_summary: bool = True,
) -> None:
    """`validate_file` that raises `ValidationError` on any mismatch."""
    errors = validate_file(source, descriptor, records, print_summary=print_summary)
    if errors:
        joined = "\n  - ".join(errors)
        raise ValidationError(
            f"{source}: {len(errors)} mismatch(es) between descriptor and output:\n"
            f"  - {joined}"
        )


def _print_summary(
    source: str,
    expected: dict[str, _ExpectedFormat],
    records: list[ERC20DisplayFormat],
    expected_deployments: int,
    rows: list[tuple[str, str, str, str]],
) -> None:
    n_addr = len({r["address"] for r in records})
    n_chain = len({r["chain_id"] for r in records})
    print(f"\n=== {source} ===")
    print(
        f"  signatures: {len(expected)}   deployments: {expected_deployments}   "
        f"records: {len(records)}   addresses: {n_addr}   chains: {n_chain}"
    )
    if not rows:
        return
    w_sel = max(len("selector"), *(len(r[0]) for r in rows))
    w_name = max(len("name"), *(len(r[1]) for r in rows))
    print(f"  {'selector':<{w_sel}}  {'name':<{w_name}}  {'fields':>6}  status")
    for sel, name, nfields, status in rows:
        print(f"  {sel:<{w_sel}}  {name:<{w_name}}  {nfields:>6}  {status}")


def validate_path(path: Path, *, print_summary: bool = True) -> None:
    """Load a descriptor + re-run the extractor on it, then cross-check.

    Convenience for the standalone CLI; the pipeline calls `validate_file_strict`
    directly with records it already produced.
    """
    from .erc7730 import load_display_formats

    descriptor = load_descriptor(path)
    records = load_display_formats(path)
    source = f"{path.parent.name}/{path.name}"
    validate_file_strict(source, descriptor, records, print_summary=print_summary)


@click.command(name="validate-display-formats")
@click.argument(
    "target",
    required=False,
    type=click.Path(exists=True, path_type=Path),
)
def validate_display_formats(target: Path | None) -> None:
    """Cross-check extracted display formats against their source descriptors.

    Re-derives, independently of the extractor, the records each ERC-7730
    descriptor should produce and asserts the extractor's output matches —
    signatures, deployment coverage, and per-field label / formatter / index
    path / token path. Prints a summary table per file and exits non-zero on the
    first mismatch.

    TARGET may be a single `calldata-*.json` descriptor or a directory tree to
    scan; defaults to the whole registry submodule.
    """
    from .erc7730 import UnsupportedFeature, load_display_formats

    # Imported lazily to avoid a circular import at module load.
    from .download import DISPLAY_FORMATS_PATH

    if target is None:
        target = DISPLAY_FORMATS_PATH

    if target.is_file():
        paths = [target]
    else:
        paths = sorted(target.glob("*/calldata-*.json"))

    scanned = validated = skipped = 0
    failures = 0
    for path in paths:
        if "tests" in path.parts:
            continue
        scanned += 1
        try:
            records = load_display_formats(path)
        except UnsupportedFeature:
            skipped += 1
            continue
        except Exception as e:  # noqa: BLE001 — report and keep scanning
            click.echo(f"failed to parse {path}: {e}", err=True)
            failures += 1
            continue
        if not records:
            continue
        source = f"{path.parent.name}/{path.name}"
        try:
            validate_file_strict(source, load_descriptor(path), records)
            validated += 1
        except ValidationError as e:
            click.echo(f"\n{e}", err=True)
            failures += 1

    click.echo(
        f"\nscanned {scanned} file(s): {validated} validated, "
        f"{skipped} skipped (unsupported), {failures} failed"
    )
    if failures:
        sys.exit(1)
