"""PList (parameter list) JSON loader and address resolver.

The Warema WMS Studio Pro ships per-product per-device JSON files that
describe the memory layout of each motor/actuator's parameter block.
Each leaf parameter has a fully-qualified name (FQN), a block number and
a block address.

For our integration we bundle the relevant decrypted JSONs under
``custom_components/warema_wms/plists/`` and use them at runtime to know
where in the device's parameter block each user-visible value lives.

Currently we only need block 38 addresses for the 6 firmware parameters
exposed in the options-flow ``firmware_params`` step:

  common.isAbsent                        -> block 38 addr 1
  manualOperation.settingDown.position   -> block 38 addr 301
  manualOperation.settingDown.slatAngle  -> block 38 addr 302
  manualOperation.dwellTimeManualScene   -> block 38 addr 305
  scene.scene0.position                  -> block 38 addr 307
  scene.scene0.slatAngle                 -> block 38 addr 308

The PList is the authoritative source - we look these up by FQN rather
than hardcoding the numbers so a future device variant with a different
layout can be supported by just dropping in its PList.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

# Where bundled PList files live, relative to this module.
_PLIST_DIR = os.path.join(os.path.dirname(__file__), "..", "plists")

# FQNs we resolve into block/addr pairs for the firmware-params flow.
# Each FQN is a path through the PList JSON tree.
FQN_COMMON_IS_ABSENT = "common/isAbsent"
FQN_MANUAL_POSITION = "manualOperation/settingDown/position"
FQN_MANUAL_SLAT_ANGLE = "manualOperation/settingDown/slatAngle"
FQN_MANUAL_DWELL_TIME = "manualOperation/dwellTimeManualScene"
FQN_COMFORT_POSITION = "scene/scene0/position"
FQN_COMFORT_SLAT_ANGLE = "scene/scene0/slatAngle"

# Default PList filename for the only currently-supported combination:
# Zwischenstecker (wms-plug-receiver-v3) running an ExternalVenetianBlind
# product (E100AF/AFA6 variant). Other combinations can be added later by
# bundling more JSON files and extending the selection logic.
DEFAULT_PRODUCT_PLIST = "E100AF-AFA6-wms-plug-receiver-v3.plist.json"


@dataclass(frozen=True)
class ParamAddr:
    """Address record for one leaf parameter."""

    block: int
    addr: int
    data_type_id: Optional[int] = None
    default_value: Optional[int] = None

    def __str__(self) -> str:
        return f"block={self.block} addr={self.addr}"


def _iter_leaf_params(
    node: Any,
    path: str = "",
    inherited_block: Optional[int] = None,
) -> Iterator[Tuple[str, ParamAddr]]:
    """Walk a PList JSON tree and yield ``(fqn, ParamAddr)`` for every leaf.

    A "leaf" is a dict that has a ``blockAddress$`` key. Inner dicts (no
    blockAddress$) are recursed into. The block number can be specified at
    any level via ``blockNumber$`` and is inherited by descendants unless
    they override it.

    Keys starting with ``$`` (e.g. ``blockNumber$``, ``isReadOnly$``) are
    metadata and not children.
    """
    if not isinstance(node, dict):
        return

    # Update inherited block if this node sets one.
    block = node.get("blockNumber$", inherited_block)

    # A leaf record has blockAddress$ at this level.
    if "blockAddress$" in node and isinstance(node["blockAddress$"], int):
        addr = node["blockAddress$"]
        if block is not None:
            yield (
                path,
                ParamAddr(
                    block=block,
                    addr=addr,
                    data_type_id=node.get("dataTypeId$"),
                    default_value=node.get("defaultValue$"),
                ),
            )
        # Some "leaf" nodes also carry sub-leaves (e.g. shared dataTypeId$
        # with per-direction blockAddress$ siblings - see duskControl's
        # releaseTimeMin in the trace). Fall through to also recurse.

    for key, value in node.items():
        if not isinstance(key, str) or key.endswith("$"):
            continue
        sub_path = f"{path}/{key}" if path else key
        yield from _iter_leaf_params(value, sub_path, block)


class PList:
    """A parsed parameter-list with FQN -> ParamAddr lookup."""

    def __init__(self, source_path: str, raw: dict[str, Any]) -> None:
        self.source_path = source_path
        self.raw = raw
        self.by_fqn: Dict[str, ParamAddr] = {}
        for fqn, addr in _iter_leaf_params(raw):
            # Earlier wins for duplicates - lets a more specific PList override
            # a base one if we ever start merging.
            self.by_fqn.setdefault(fqn, addr)

    def get(self, fqn: str) -> Optional[ParamAddr]:
        """Look up a parameter by its slash-separated FQN."""
        return self.by_fqn.get(fqn)

    def require(self, fqn: str) -> ParamAddr:
        """Like ``get`` but raises ``KeyError`` if the FQN is unknown."""
        addr = self.get(fqn)
        if addr is None:
            raise KeyError(
                f"FQN {fqn!r} not found in PList {os.path.basename(self.source_path)}"
            )
        return addr

    def __repr__(self) -> str:
        return (
            f"PList(source={os.path.basename(self.source_path)}, "
            f"params={len(self.by_fqn)})"
        )


_CACHED_PLISTS: Dict[str, PList] = {}


def load_plist(filename: str) -> PList:
    """Load (and cache) a bundled PList JSON.

    ``filename`` is the bare file name (e.g.
    ``"E100AF-AFA6-wms-plug-receiver-v3.plist.json"``) and is looked up under
    the ``plists/`` directory next to this module.
    """
    cached = _CACHED_PLISTS.get(filename)
    if cached is not None:
        return cached
    full_path = os.path.join(_PLIST_DIR, filename)
    with open(full_path, encoding="utf-8") as f:
        raw = json.load(f)
    pl = PList(full_path, raw)
    _CACHED_PLISTS[filename] = pl
    _LOGGER.debug("Loaded PList %s with %d parameters", filename, len(pl.by_fqn))
    return pl


def select_plist_for_device(device_type: str, sw_version: str | None = None) -> PList:
    """Return the PList to use for a given device.

    Currently we ship one PList that covers all supported devices
    (Zwischenstecker running ExternalVenetianBlind v3). Future device
    variants can extend this routing.
    """
    # device_type "21" = PLUG_RECEIVER / Zwischenstecker
    # device_type "20" = ACTUATOR_UP
    # device_type "2E" = ACTUATOR_230V_UP
    # All three currently route to the same PList until proven otherwise.
    return load_plist(DEFAULT_PRODUCT_PLIST)
