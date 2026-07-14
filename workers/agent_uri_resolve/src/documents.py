"""uri_documents lookup / upsert by uri_hash."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

LOOKUP_SQL = """
SELECT id, document, status, source_gateway
FROM erc_8004.uri_documents
WHERE uri_hash = %(uri_hash)s
LIMIT 1
"""

TOUCH_SQL = """
UPDATE erc_8004.uri_documents
SET
  last_accessed_at = NOW(),
  fetch_count = COALESCE(fetch_count, 0) + 1,
  updated_at = NOW()
WHERE id = %(id)s
"""

UPSERT_SQL = """
INSERT INTO erc_8004.uri_documents AS ud (
  uri_hash,
  uri,
  cid,
  document,
  status,
  fetched_at,
  expires_at,
  source_gateway,
  fetch_count,
  last_accessed_at,
  created_at,
  updated_at
) VALUES (
  %(uri_hash)s,
  %(uri)s,
  %(cid)s,
  %(document)s::jsonb,
  'valid',
  NOW(),
  NOW() + interval '7 days',
  %(source_gateway)s,
  1,
  NOW(),
  NOW(),
  NOW()
)
ON CONFLICT (uri_hash) DO UPDATE SET
  uri = EXCLUDED.uri,
  document = EXCLUDED.document,
  status = 'valid',
  fetched_at = NOW(),
  expires_at = NOW() + interval '7 days',
  source_gateway = EXCLUDED.source_gateway,
  fetch_count = COALESCE(ud.fetch_count, 0) + 1,
  last_accessed_at = NOW(),
  updated_at = NOW()
RETURNING id
"""

CID_RE = re.compile(r"([a-zA-Z0-9]{46,64})")


def extract_cid(uri: str) -> str | None:
    match = CID_RE.search(uri)
    return match.group(1) if match else None


def uri_hash(uri: str) -> str:
    """md5 hex of original URI — matches uri_documents.uri_hash UNIQUE key."""
    return hashlib.md5((uri or "").encode("utf-8")).hexdigest()


def dumps_document(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, default=str)
