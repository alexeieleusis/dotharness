from __future__ import annotations

from dataclasses import dataclass

_FILE_SCOPE_IN_BAND = range(5, 11)
_FILE_SCOPE_CEILING = 15

_WORD_COUNT_IN_BAND = range(500, 1501)
_WORD_COUNT_OBSERVED_MIN = 532
_WORD_COUNT_OBSERVED_MAX = 2100


@dataclass(frozen=True)
class SizingResult:
    count: int
    in_band: bool
    over_ceiling: bool = False
    note: str | None = None


def check_file_scope(paths: list[str]) -> SizingResult:
    count = len(paths)
    in_band = count in _FILE_SCOPE_IN_BAND
    over_ceiling = count > _FILE_SCOPE_CEILING
    note = None
    if over_ceiling:
        note = f"{count} files exceeds the hard ceiling of {_FILE_SCOPE_CEILING}"
    elif not in_band:
        note = (
            f"{count} files is outside the in-band range of {_FILE_SCOPE_IN_BAND.start}-{_FILE_SCOPE_IN_BAND.stop - 1}"
        )

    return SizingResult(count=count, in_band=in_band, over_ceiling=over_ceiling, note=note)


def check_word_count(text: str) -> SizingResult:
    count = len(text.split())
    in_band = count in _WORD_COUNT_IN_BAND
    note = None
    if not in_band and (count < _WORD_COUNT_OBSERVED_MIN or count > _WORD_COUNT_OBSERVED_MAX):
        note = (
            f"{count} words falls outside the observed range of {_WORD_COUNT_OBSERVED_MIN}-{_WORD_COUNT_OBSERVED_MAX}"
        )

    return SizingResult(count=count, in_band=in_band, note=note)
