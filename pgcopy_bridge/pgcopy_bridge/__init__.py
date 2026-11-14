"""Bidirectional PostgreSQL binary COPY <-> typed JSONL converter.

Public API::

    from pgcopy_bridge import (
        load_catalogue, Limits,
        decode_file, encode_file, inspect_file,
        decode_bytes, encode_bytes,
    )

All conversion is driven by an explicit column catalogue; types are never
inferred from content.
"""

from .catalogue import (  # noqa: F401
    Catalogue,
    Column,
    TypeSpec,
    catalogue_from_mapping,
    load_catalogue,
    normalise_type,
    supported_type_names,
)
from .errors import (  # noqa: F401
    CopyFormatError,
    DescriptorError,
    LimitsError,
    PgCopyError,
    RecordError,
    ValueFormatError,
)
from .library import (  # noqa: F401
    ColumnStats,
    DecodeResult,
    InspectResult,
    decode_bytes,
    decode_file,
    encode_bytes,
    encode_file,
    inspect_file,
)
from .limits import Limits  # noqa: F401

__version__ = "1.0.0"
