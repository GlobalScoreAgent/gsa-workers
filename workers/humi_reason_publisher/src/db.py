"""Postgres access for humi_reason_publisher."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

from pillar_spec import PILLARS, pillar_columns

logger = logging.getLogger("humi_reason_publisher")

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

CLAIM_SQL = """
SELECT agent_id, version, reason_content_sha256
FROM index_humi.claim_reason_publish(
  %(limit)s,
  %(worker_id)s,
  %(stale_seconds)s
)
"""
COMPLETE_SQL = """
SELECT index_humi.complete_reason_publish(%(rows)s::jsonb)
"""

# Contextos de render Stage 2: mismos JOINs que agent_pillar_*_calculate usan
# para armar reasons (sin recalcular scores).
HISTORY_CONTEXT_SQL = """
SELECT
  a.id AS agent_id,
  a.owner_wallet_id AS owner_id,
  a.owner_since_at,
  a.owner_changes,
  a.on_chain_created_at,
  ow.wallet_created_at,
  ow.owner_chains,
  COALESCE(osa.total_agents, 0) AS owner_portafolio_agent_total,
  COALESCE(osa.active_agents, 0) AS owner_portafolio_agent_active_total,
  COALESCE(osa.metadata_category_distribution, '{}'::jsonb)
    AS owner_portafolio_agent_metadata_richness,
  jsonb_build_object(
    'total_agents_analyzed', COALESCE(oww.total_agents_analyzed, 0),
    'duplication_metadata_count', COALESCE(oww.duplication_metadata_count, 0),
    'multi_agent_wallet_count', COALESCE(oww.multi_agent_wallet_count, 0),
    'dummy_metadata_count', COALESCE(oww.dummy_metadata_count, 0),
    'attestations_spam_count', COALESCE(oww.attestations_spam_count, 0),
    'external_audit_warning_count', COALESCE(oww.external_audit_warning_count, 0),
    'high_revocations_count', COALESCE(oww.high_revocations_count, 0),
    'owner_inactive_agents_count', COALESCE(oww.owner_inactive_agents_count, 0),
    'high_ownership_churn_count', COALESCE(oww.high_ownership_churn_count, 0),
    'transactional_same_as_owner_count', COALESCE(oww.transactional_same_as_owner_count, 0),
    'lower_realness_count', COALESCE(oww.lower_realness_count, 0),
    'lower_metadata_richness_count', COALESCE(oww.lower_metadata_richness_count, 0),
    'total_agents_with_warnings', COALESCE(oww.total_agents_with_warnings, 0)
  ) AS owner_portafolio_agent_warnings,
  jsonb_build_object(
    'total_valid_audits', COALESCE(oea.total_valid_audits, 0),
    'agents_with_poor_score', COALESCE(oea.agents_with_poor_score, 0),
    'agents_with_deficient_score', COALESCE(oea.agents_with_deficient_score, 0),
    'agents_with_normal_score', COALESCE(oea.agents_with_normal_score, 0),
    'agents_with_good_score', COALESCE(oea.agents_with_good_score, 0),
    'agents_with_excellent_score', COALESCE(oea.agents_with_excelent_score, 0)
  ) AS owner_portafolio_agent_external_audits,
  jsonb_build_object(
    'total_valid_attestations', COALESCE(oat.total_valid_attestations, 0),
    'agents_with_poor_score', COALESCE(oat.agents_with_poor_score, 0),
    'agents_with_deficient_score', COALESCE(oat.agents_with_deficient_score, 0),
    'agents_with_normal_score', COALESCE(oat.agents_with_normal_score, 0),
    'agents_with_good_score', COALESCE(oat.agents_with_good_score, 0),
    'agents_with_excellent_score', COALESCE(oat.agents_with_excellent_score, 0)
  ) AS owner_portafolio_agent_attestations,
  jsonb_build_object(
    'total_agents_with_execution_data', COALESCE(oex.total_agents_with_execution_data, 0),
    'agents_with_no_executions', COALESCE(oex.agents_with_no_executions, 0),
    'agents_with_executions', COALESCE(oex.agents_with_executions, 0),
    'average_executions_per_agent', COALESCE(oex.average_executions_per_agent, 0),
    'total_executions_count', COALESCE(oex.total_executions_count, 0)
  ) AS owner_portafolio_agent_on_chain_executions,
  jsonb_build_object(
    'total_protocol_activities', COALESCE(opa.total_protocol_activities, 0),
    'agents_with_poor_score', COALESCE(opa.agents_with_poor_score, 0),
    'agents_with_deficient_score', COALESCE(opa.agents_with_deficient_score, 0),
    'agents_with_normal_score', COALESCE(opa.agents_with_normal_score, 0),
    'agents_with_good_score', COALESCE(opa.agents_with_good_score, 0),
    'agents_with_excellent_score', COALESCE(opa.agents_with_excellent_score, 0)
  ) AS owner_portafolio_agent_activity_protocols,
  jsonb_build_object(
    'agents_with_services', COALESCE(os.agents_with_services, 0),
    'agents_with_no_services', COALESCE(os.agents_with_no_services, 0),
    'agents_with_one_service', COALESCE(os.agents_with_one_service, 0),
    'agents_with_two_services', COALESCE(os.agents_with_two_services, 0),
    'agents_with_three_services', COALESCE(os.agents_with_three_services, 0),
    'agents_with_four_services', COALESCE(os.agents_with_four_services, 0),
    'agents_with_five_or_more_services', COALESCE(os.agents_with_five_or_more_services, 0),
    'agents_with_specialized_services', COALESCE(os.agents_with_specialized_services, 0),
    'agents_with_x402_support', COALESCE(os.agents_with_x402_support, 0)
  ) AS owner_portafolio_agents_metadata_services,
  (
    jsonb_typeof(ow.owner_chains) = 'array'
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(ow.owner_chains) elem
      WHERE elem->>'wallet_type' = 'active'
    )
  ) AS owner_has_active_wallet_in_chain_info
FROM erc_8004.agents a
LEFT JOIN erc_8004.owner_summary_wallets ow ON ow.owner_wallet_id = a.owner_wallet_id
LEFT JOIN erc_8004.owner_summary_agents osa ON osa.owner_wallet_id = a.owner_wallet_id
LEFT JOIN erc_8004.owner_summary_agent_warnings oww ON oww.owner_wallet_id = a.owner_wallet_id
LEFT JOIN erc_8004.owner_summary_agent_external_audits oea
  ON oea.owner_wallet_id = a.owner_wallet_id
LEFT JOIN erc_8004.owner_summary_agent_attestations oat
  ON oat.owner_wallet_id = a.owner_wallet_id
LEFT JOIN erc_8004.owner_summary_agent_executions oex
  ON oex.owner_wallet_id = a.owner_wallet_id
LEFT JOIN erc_8004.owner_summary_agent_protocol_activity opa
  ON opa.owner_wallet_id = a.owner_wallet_id
LEFT JOIN erc_8004.owner_summary_agent_services os ON os.owner_wallet_id = a.owner_wallet_id
WHERE a.id = ANY(%(agent_ids)s)
"""

INFORMATION_CONTEXT_SQL = """
SELECT
  a.id AS agent_id,
  a.name,
  a.description,
  a.image_url,
  COALESCE(ps.profiles, '{}'::jsonb) AS profiles,
  COALESCE(ssi.mcp_count, 0) AS mcp_count,
  COALESCE(ssi.a2a_count, 0) AS a2a_count,
  COALESCE(ssi.programmatic_count, 0) AS programmatic_count,
  ssd.web,
  ssd.email,
  COALESCE(s.services_breakdown, '{}'::jsonb) AS services_breakdown,
  COALESCE(vm.verification_methods_data, '[]'::jsonb) AS verification_methods,
  COALESCE(tch.technology_stacks_data, '[]'::jsonb) AS technology_stacks,
  COALESCE(s.technical_tools_data, '[]'::jsonb) AS technical_tools,
  COALESCE(s.technical_capabilites_data, '[]'::jsonb) AS technical_capabilities,
  COALESCE(x402.x402s, '[]'::jsonb) AS x402s
FROM erc_8004.agents a
LEFT JOIN erc_8004.agent_summary_services s ON s.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_services_information ssi ON ssi.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_services_detailed ssd ON ssd.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_verification_methods vm ON vm.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_metadata_technology tch ON tch.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_profiles ps ON ps.agent_id = a.id
LEFT JOIN (
  SELECT agent_id, jsonb_agg(jsonb_build_object('type', type)) AS x402s
  FROM erc_8004.agent_metadata_x402
  GROUP BY agent_id
) x402 ON x402.agent_id = a.id
WHERE a.id = ANY(%(agent_ids)s)
"""

# Measure/usage: mismos jsonb que arma el FOR del calculate (inputs del renderer).
MEASURE_CONTEXT_SQL = """
SELECT
  a.id AS agent_id,
  jsonb_build_object(
    'score', COALESCE(a.metadata_richness_score, 0),
    'category', COALESCE(a.metadata_category, 'unknown')
  ) AS metadata_richness_information,
  jsonb_build_object(
    'nonce_current', COALESCE(asg.nonce, 0),
    'nonce_delta_1month', COALESCE(asg.nonce_delta_30_days, 0)
  ) AS wallet_transaction_data,
  jsonb_build_object(
    'has_high_value_chain_presence', COALESCE(asg.has_high_value_chain_presence, false),
    'weighted_multichain_activity_score', COALESCE(asg.weighted_multichain_activity_score, 0),
    'shallow_multichain_spread', COALESCE(asg.shallow_multichain_spread, false),
    'active_chains_count', COALESCE(asg.active_chains_count, 0)
  ) AS multichain_data,
  jsonb_build_object(
    'total_count', COALESCE(ast.attestations_total_count, 0),
    'valid_count', COALESCE(ast.attestations_valid_count, 0),
    'avg_score', COALESCE(ast.attestation_score_avg, 0)
  ) AS attestations_summary,
  jsonb_build_object(
    'total_count', COALESCE(oce.on_chain_executions_count, 0),
    'valid_count', COALESCE(oce.on_chain_executions_valid_count, 0)
  ) AS on_chain_executions_summary,
  jsonb_build_object(
    'total_count', COALESCE(ocf.on_chain_feedbacks_count, 0),
    'valid_count', COALESCE(ocf.on_chain_feedbacks_valid_count, 0)
  ) AS on_chain_feedbacks_summary,
  jsonb_build_object(
    'total_count', COALESCE(opa.protocol_activity_count, 0),
    'valid_count', COALESCE(opa.protocol_activity_valid_count, 0),
    'valid_payment_count', COALESCE(opa.protocol_activity_valid_payment_count, 0),
    'avg_score', COALESCE(opa.protocol_activity_score, 0)
  ) AS protocol_activity_summary,
  jsonb_build_object(
    'total_count', COALESCE(ea.external_source_count, 0),
    'valid_count', COALESCE(ea.external_source_valid_count, 0),
    'avg_score', COALESCE(ea.external_source_score, 0)
  ) AS external_audits_summary,
  COALESCE(wrn.warnings_data, '[]'::jsonb) AS warnings_summary,
  COALESCE(
    (
      SELECT jsonb_build_object(
        'identity_score', ai.identity_score,
        'identity_stage', ai.identity_stage,
        'updated_at', ai.updated_at
      )
      FROM erc_8004.agent_analysis_identity ai
      WHERE ai.agent_id = a.id AND ai.is_revoke = false
      ORDER BY ai.updated_at DESC
      LIMIT 1
    ),
    '{}'::jsonb
  ) AS identity_analysis
FROM erc_8004.agents a
LEFT JOIN erc_8004.agent_summary_general asg ON asg.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_attestations ast ON ast.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_on_chain_executions oce ON oce.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_on_chain_feedbacks ocf ON ocf.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_protocol_activity opa ON opa.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_external_audits ea ON ea.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_warning wrn ON wrn.agent_id = a.id
WHERE a.id = ANY(%(agent_ids)s)
"""

USAGE_CONTEXT_SQL = """
SELECT
  a.id AS agent_id,
  a.on_chain_created_at AS agent_created_at,
  a.on_chain_created_at,
  jsonb_build_object(
    'nonce_current', COALESCE(asg.nonce, 0),
    'first_nonce', COALESCE(asg.first_nonce, 0),
    'nonce_history_span_days', COALESCE(asg.nonce_history_span_days, 0),
    'nonce_delta_7_days', COALESCE(asg.nonce_delta_7_days, 0),
    'nonce_delta_15_days', COALESCE(asg.nonce_delta_15_days, 0),
    'nonce_delta_30_days', COALESCE(asg.nonce_delta_30_days, 0)
  ) AS wallet_tx_summary,
  jsonb_build_object(
    'valuable_chains_count', COALESCE(asg.valuable_chains_count, 0),
    'shallow_multichain_spread', COALESCE(asg.shallow_multichain_spread, false),
    'has_high_value_chain_presence', COALESCE(asg.has_high_value_chain_presence, false),
    'usage_concentration_ratio', COALESCE(asg.usage_concentration_ratio, 0)
  ) AS multichain_data,
  jsonb_build_object(
    'total_count', COALESCE(ast.attestations_total_count, 0),
    'valid_count', COALESCE(ast.attestations_valid_count, 0),
    'revoke_count', COALESCE(ast.attestations_revoke_count, 0),
    'spam_count', COALESCE(ast.attestation_spam_count, 0),
    'avg_score', ast.attestation_score_avg
  ) AS attestations_summary,
  jsonb_build_object(
    'total_count', COALESCE(oce.on_chain_executions_count, 0),
    'valid_count', COALESCE(oce.on_chain_executions_valid_count, 0),
    'revoke_count', COALESCE(oce.on_chain_executions_revoke_count, 0)
  ) AS on_chain_executions_summary,
  jsonb_build_object(
    'total_count', COALESCE(ocf.on_chain_feedbacks_count, 0),
    'valid_count', COALESCE(ocf.on_chain_feedbacks_valid_count, 0),
    'revoke_count', COALESCE(ocf.on_chain_feedbacks_revoke_count, 0),
    'avg_score', ocf.on_chain_feedbacks_avg_score
  ) AS on_chain_feedbacks_summary,
  jsonb_build_object(
    'total_count', COALESCE(opa.protocol_activity_count, 0),
    'valid_count', COALESCE(opa.protocol_activity_valid_count, 0),
    'revoke_count', COALESCE(opa.protocol_activity_revoke_count, 0),
    'valid_payment_count', COALESCE(opa.protocol_activity_valid_payment_count, 0),
    'avg_score', opa.protocol_activity_score
  ) AS protocol_activity_summary,
  jsonb_build_object(
    'total_count', COALESCE(com.comments_total_count, 0),
    'valid_count', COALESCE(com.comments_valid_count, 0),
    'revoke_count', COALESCE(com.comments_revoke_count, 0)
  ) AS comments_summary
FROM erc_8004.agents a
LEFT JOIN erc_8004.agent_summary_general asg ON asg.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_attestation_last_30_days ast ON ast.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_on_chain_executions_last_30_days oce ON oce.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_on_chain_feedbacks_last_30_days ocf ON ocf.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_protocol_activity_last_30_days opa ON opa.agent_id = a.id
LEFT JOIN erc_8004.agent_summary_comments_last_30_days com ON com.agent_id = a.id
WHERE a.id = ANY(%(agent_ids)s)
"""

CONTEXT_SQL_BY_TABLE = {
    "pillar_history": HISTORY_CONTEXT_SQL,
    "pillar_information": INFORMATION_CONTEXT_SQL,
    "pillar_measures": MEASURE_CONTEXT_SQL,
    "pillar_usage": USAGE_CONTEXT_SQL,
}

T = TypeVar("T")


def _pillar_sql(include_reasons: bool) -> dict[str, str]:
    return {
        pillar.table: (
            "SELECT agent_id, {cols} FROM index_humi.{table} "
            "WHERE agent_id = ANY(%(agent_ids)s)"
        ).format(
            cols=", ".join(pillar_columns(pillar, include_reasons=include_reasons)),
            table=pillar.table,
        )
        for pillar in PILLARS
    }


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None

    def connect(self) -> None:
        self._conn = psycopg.connect(self._dsn, row_factory=dict_row)
        with self._conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _reconnect(self) -> None:
        logger.warning("Reconnecting to Postgres after connection failure")
        self.close()
        self.connect()

    def ensure_connected(self) -> None:
        if self._conn is None or self._conn.closed:
            self._reconnect()

    def _safe_rollback(self) -> None:
        if self._conn is None or self._conn.closed:
            return
        try:
            self._conn.rollback()
        except Exception:
            pass

    def _run_with_db_retry(self, operation: str, fn: Callable[[], T]) -> T:
        last_exc: Exception | None = None
        for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
            try:
                self.ensure_connected()
                return fn()
            except RETRYABLE_DB_EXCEPTIONS as exc:
                last_exc = exc
                self._safe_rollback()
                if attempt >= CLAIM_MAX_ATTEMPTS:
                    break
                delay = CLAIM_RETRY_BASE_SECONDS * attempt
                if isinstance(exc, _NO_RECONNECT_EXCEPTIONS):
                    logger.warning(
                        "%s attempt %s/%s retryable DB error (%s); retrying in %.1fs",
                        operation,
                        attempt,
                        CLAIM_MAX_ATTEMPTS,
                        exc.__class__.__name__,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "%s attempt %s/%s connection error (%s); reconnecting in %.1fs",
                        operation,
                        attempt,
                        CLAIM_MAX_ATTEMPTS,
                        exc.__class__.__name__,
                        delay,
                    )
                    time.sleep(delay)
                    self._reconnect()
            except Exception:
                self._safe_rollback()
                raise

        assert last_exc is not None
        raise last_exc

    def claim_rows(
        self,
        worker_id: str,
        limit: int,
        stale_seconds: int,
    ) -> list[dict[str, Any]]:
        def _claim() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    CLAIM_SQL,
                    {
                        "limit": limit,
                        "worker_id": worker_id,
                        "stale_seconds": stale_seconds,
                    },
                )
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("claim", _claim)

    def fetch_pillars(
        self,
        agent_ids: list[int],
        *,
        include_reasons: bool = True,
    ) -> dict[int, dict[str, dict[str, Any] | None]]:
        """Devuelve {agent_id: {tabla_pilar: fila|None}} para todo el lote."""
        if not agent_ids:
            return {}

        sql_by_table = _pillar_sql(include_reasons)

        def _fetch() -> dict[int, dict[str, dict[str, Any] | None]]:
            assert self._conn is not None
            by_agent: dict[int, dict[str, dict[str, Any] | None]] = {
                agent_id: {pillar.table: None for pillar in PILLARS} for agent_id in agent_ids
            }
            with self._conn.cursor() as cur:
                for table, sql in sql_by_table.items():
                    cur.execute(sql, {"agent_ids": agent_ids})
                    for row in cur.fetchall():
                        agent_id = int(row.pop("agent_id"))
                        slot = by_agent.get(agent_id)
                        if slot is not None:
                            slot[table] = row
            self._conn.commit()
            return by_agent

        return self._run_with_db_retry("fetch_pillars", _fetch)

    def fetch_render_contexts(
        self, agent_ids: list[int]
    ) -> dict[int, dict[str, dict[str, Any]]]:
        """Inputs por agente y tabla de pilar para src/render/."""
        if not agent_ids:
            return {}

        def _fetch() -> dict[int, dict[str, dict[str, Any]]]:
            assert self._conn is not None
            by_agent: dict[int, dict[str, dict[str, Any]]] = {
                agent_id: {} for agent_id in agent_ids
            }
            with self._conn.cursor() as cur:
                for table, sql in CONTEXT_SQL_BY_TABLE.items():
                    try:
                        cur.execute(sql, {"agent_ids": agent_ids})
                    except Exception:
                        self._safe_rollback()
                        raise
                    for row in cur.fetchall():
                        agent_id = int(row.pop("agent_id"))
                        slot = by_agent.get(agent_id)
                        if slot is not None:
                            slot[table] = dict(row)
            self._conn.commit()
            return by_agent

        return self._run_with_db_retry("fetch_render_contexts", _fetch)

    def complete(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0

        def _complete() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(COMPLETE_SQL, {"rows": json.dumps(rows)})
                result = cur.fetchone()
            self._conn.commit()
            return int(result["complete_reason_publish"]) if result else 0

        return self._run_with_db_retry("complete", _complete)
