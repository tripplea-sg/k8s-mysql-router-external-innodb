#!/usr/bin/env python3
"""
List MySQL Router deployments and pod status.

Examples:
  python scripts/list_mysql_router_status.py
  python scripts/list_mysql_router_status.py -n mysql-router
  python scripts/list_mysql_router_status.py --label-selector app=mysql-router
  python scripts/list_mysql_router_status.py --output json
  python scripts/list_mysql_router_status.py --output json --compact
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.config.config_exception import ConfigException


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List MySQL Router deployments and pod status."
    )
    parser.add_argument(
        "-n",
        "--namespace",
        help="Namespace to search. If omitted, search all namespaces.",
    )
    parser.add_argument(
        "--label-selector",
        help="Optional Kubernetes label selector to narrow the deployments returned by the API.",
    )
    parser.add_argument(
        "--match",
        default="mysql-router",
        help=(
            "Case-insensitive text used to identify MySQL Router deployments "
            "(default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--output",
        choices=["table", "json"],
        default="table",
        help="Output format: table or json (default: table).",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of pretty-printed JSON. Only used with --output json.",
    )
    return parser.parse_args()


def load_cluster_config() -> str:
    try:
        config.load_kube_config()
        return "kubeconfig"
    except ConfigException:
        pass

    try:
        config.load_incluster_config()
        return "incluster"
    except ConfigException as exc:
        raise SystemExit(
            "Could not load Kubernetes configuration. "
            "Tried local kubeconfig and in-cluster config."
        ) from exc


def safe_int(value: Optional[int]) -> int:
    return int(value or 0)


def to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def age_seconds(created_at: Optional[datetime]) -> Optional[int]:
    if created_at is None:
        return None
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    delta = now - created_at
    return max(0, int(delta.total_seconds()))


def age_human(created_at: Optional[datetime]) -> Optional[str]:
    seconds = age_seconds(created_at)
    if seconds is None:
        return None

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def ready_condition(pod: client.V1Pod) -> bool:
    for condition in pod.status.conditions or []:
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def ready_counts(pod: client.V1Pod) -> tuple[int, int]:
    statuses = pod.status.container_statuses or []
    total = len(pod.spec.containers or [])
    ready = sum(1 for status in statuses if status.ready)
    return ready, total


def ready_ratio(pod: client.V1Pod) -> str:
    ready, total = ready_counts(pod)
    return f"{ready}/{total}"


def restart_count(pod: client.V1Pod) -> int:
    total = 0
    for status in pod.status.init_container_statuses or []:
        total += int(status.restart_count or 0)
    for status in pod.status.container_statuses or []:
        total += int(status.restart_count or 0)
    return total


def pod_reason(pod: client.V1Pod) -> str:
    if pod.metadata.deletion_timestamp:
        return "Terminating"

    if pod.status.reason:
        return str(pod.status.reason)

    statuses = list(pod.status.init_container_statuses or []) + list(
        pod.status.container_statuses or []
    )

    for status in statuses:
        state = status.state
        if not state:
            continue
        if state.waiting and state.waiting.reason:
            return str(state.waiting.reason)
        if state.terminated and state.terminated.reason:
            return str(state.terminated.reason)

    return "-"


def object_owner_uids(obj) -> set[str]:
    return {
        ref.uid
        for ref in (obj.metadata.owner_references or [])
        if getattr(ref, "uid", None)
    }


def deployment_looks_like_mysql_router(
    deployment: client.V1Deployment,
    match_text: Optional[str],
) -> bool:
    haystack_parts: List[str] = [deployment.metadata.name or ""]

    selector_labels = {}
    if deployment.spec and deployment.spec.selector:
        selector_labels = deployment.spec.selector.match_labels or {}

    template_labels = {}
    if (
        deployment.spec
        and deployment.spec.template
        and deployment.spec.template.metadata
    ):
        template_labels = deployment.spec.template.metadata.labels or {}

    for mapping in (
        deployment.metadata.labels or {},
        selector_labels,
        template_labels,
    ):
        for key, value in mapping.items():
            haystack_parts.append(str(key))
            haystack_parts.append(str(value))

    containers = []
    if deployment.spec and deployment.spec.template and deployment.spec.template.spec:
        containers = deployment.spec.template.spec.containers or []

    for container_spec in containers:
        haystack_parts.append(container_spec.name or "")
        haystack_parts.append(container_spec.image or "")

    haystack = " ".join(haystack_parts).lower()
    probes = {
        (match_text or "").lower(),
        "mysql-router",
        "mysqlrouter",
        "community-router",
        "mysql/router",
    }
    probes.discard("")

    return any(probe in haystack for probe in probes)


def build_namespace_cache(
    apps_api: client.AppsV1Api,
    core_api: client.CoreV1Api,
    namespaces: Iterable[str],
) -> tuple[dict[str, list[client.V1ReplicaSet]], dict[str, list[client.V1Pod]]]:
    replica_sets_by_ns: dict[str, list[client.V1ReplicaSet]] = {}
    pods_by_ns: dict[str, list[client.V1Pod]] = {}

    for namespace in sorted(set(namespaces)):
        replica_sets_by_ns[namespace] = apps_api.list_namespaced_replica_set(
            namespace=namespace
        ).items
        pods_by_ns[namespace] = core_api.list_namespaced_pod(namespace=namespace).items

    return replica_sets_by_ns, pods_by_ns


def pods_for_deployment(
    deployment: client.V1Deployment,
    replica_sets: Sequence[client.V1ReplicaSet],
    pods: Sequence[client.V1Pod],
) -> list[client.V1Pod]:
    deployment_uid = deployment.metadata.uid
    replica_set_uids = {
        rs.metadata.uid
        for rs in replica_sets
        if deployment_uid in object_owner_uids(rs)
    }

    matched_pods = []
    for pod in pods:
        if replica_set_uids & object_owner_uids(pod):
            matched_pods.append(pod)

    return sorted(matched_pods, key=lambda item: item.metadata.name or "")


def state_to_dict(state) -> Optional[dict]:
    if not state:
        return None

    if state.waiting:
        return {
            "type": "waiting",
            "reason": state.waiting.reason,
            "message": state.waiting.message,
        }

    if state.running:
        return {
            "type": "running",
            "started_at": to_iso(state.running.started_at),
        }

    if state.terminated:
        return {
            "type": "terminated",
            "reason": state.terminated.reason,
            "message": state.terminated.message,
            "exit_code": state.terminated.exit_code,
            "signal": state.terminated.signal,
            "started_at": to_iso(state.terminated.started_at),
            "finished_at": to_iso(state.terminated.finished_at),
        }

    return {"type": "unknown"}


def container_status_to_dict(status) -> dict:
    return {
        "name": status.name,
        "ready": bool(status.ready),
        "restart_count": int(status.restart_count or 0),
        "image": status.image,
        "image_id": status.image_id,
        "container_id": status.container_id,
        "started": (
            bool(status.started) if getattr(status, "started", None) is not None else None
        ),
        "state": state_to_dict(status.state),
        "last_state": state_to_dict(status.last_state),
    }


def pod_conditions_to_list(pod: client.V1Pod) -> list[dict]:
    conditions = []
    for condition in pod.status.conditions or []:
        conditions.append(
            {
                "type": condition.type,
                "status": condition.status,
                "reason": condition.reason,
                "message": condition.message,
                "last_probe_time": to_iso(condition.last_probe_time),
                "last_transition_time": to_iso(condition.last_transition_time),
            }
        )
    return conditions


def deployment_conditions_to_list(deployment: client.V1Deployment) -> list[dict]:
    conditions = []
    for condition in deployment.status.conditions or []:
        conditions.append(
            {
                "type": condition.type,
                "status": condition.status,
                "reason": condition.reason,
                "message": condition.message,
                "last_update_time": to_iso(condition.last_update_time),
                "last_transition_time": to_iso(condition.last_transition_time),
            }
        )
    return conditions


def pod_to_dict(pod: client.V1Pod) -> dict:
    ready, total = ready_counts(pod)

    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "phase": pod.status.phase,
        "ready": ready_condition(pod),
        "ready_containers": ready,
        "total_containers": total,
        "restart_count": restart_count(pod),
        "reason": pod_reason(pod),
        "node_name": pod.spec.node_name,
        "pod_ip": pod.status.pod_ip,
        "host_ip": pod.status.host_ip,
        "qos_class": getattr(pod.status, "qos_class", None),
        "start_time": to_iso(pod.status.start_time),
        "created_at": to_iso(pod.metadata.creation_timestamp),
        "age_seconds": age_seconds(pod.metadata.creation_timestamp),
        "age_human": age_human(pod.metadata.creation_timestamp),
        "labels": pod.metadata.labels or {},
        "conditions": pod_conditions_to_list(pod),
        "init_containers": [
            container_status_to_dict(status)
            for status in (pod.status.init_container_statuses or [])
        ],
        "containers": [
            container_status_to_dict(status)
            for status in (pod.status.container_statuses or [])
        ],
    }


def deployment_to_dict(
    deployment: client.V1Deployment,
    deployment_pods: Sequence[client.V1Pod],
) -> dict:
    selector = {}
    if deployment.spec and deployment.spec.selector:
        selector = deployment.spec.selector.match_labels or {}

    return {
        "namespace": deployment.metadata.namespace,
        "name": deployment.metadata.name,
        "created_at": to_iso(deployment.metadata.creation_timestamp),
        "age_seconds": age_seconds(deployment.metadata.creation_timestamp),
        "age_human": age_human(deployment.metadata.creation_timestamp),
        "labels": deployment.metadata.labels or {},
        "selector": selector,
        "replicas": {
            "desired": safe_int(deployment.spec.replicas if deployment.spec else 0),
            "ready": safe_int(deployment.status.ready_replicas),
            "updated": safe_int(deployment.status.updated_replicas),
            "available": safe_int(deployment.status.available_replicas),
            "unavailable": safe_int(deployment.status.unavailable_replicas),
        },
        "conditions": deployment_conditions_to_list(deployment),
        "pod_count": len(deployment_pods),
        "pods": [pod_to_dict(pod) for pod in deployment_pods],
    }


def print_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    if not rows:
        print("(no rows)")
        return

    widths = [len(header) for header in headers]
    string_rows: List[List[str]] = []

    for row in rows:
        string_row = [str(col) for col in row]
        string_rows.append(string_row)
        for idx, col in enumerate(string_row):
            widths[idx] = max(widths[idx], len(col))

    header_line = "  ".join(
        header.ljust(widths[idx]) for idx, header in enumerate(headers)
    )
    sep_line = "  ".join("-" * widths[idx] for idx in range(len(headers)))

    print(header_line)
    print(sep_line)
    for row in string_rows:
        print("  ".join(col.ljust(widths[idx]) for idx, col in enumerate(row)))


def render_table(deployments: Sequence[client.V1Deployment], deployment_pods_map: dict[str, list[client.V1Pod]]) -> None:
    summary_rows = []
    for dep in sorted(
        deployments,
        key=lambda item: (item.metadata.namespace or "", item.metadata.name or ""),
    ):
        key = f"{dep.metadata.namespace}/{dep.metadata.name}"
        dep_pods = deployment_pods_map.get(key, [])

        summary_rows.append(
            [
                dep.metadata.namespace or "-",
                dep.metadata.name or "-",
                safe_int(dep.spec.replicas if dep.spec else 0),
                safe_int(dep.status.ready_replicas),
                safe_int(dep.status.updated_replicas),
                safe_int(dep.status.available_replicas),
                safe_int(dep.status.unavailable_replicas),
                len(dep_pods),
            ]
        )

    print("\nMySQL Router deployments\n")
    print_table(
        headers=[
            "NAMESPACE",
            "DEPLOYMENT",
            "DESIRED",
            "READY",
            "UPDATED",
            "AVAILABLE",
            "UNAVAILABLE",
            "PODS",
        ],
        rows=summary_rows,
    )

    print()
    for dep in sorted(
        deployments,
        key=lambda item: (item.metadata.namespace or "", item.metadata.name or ""),
    ):
        key = f"{dep.metadata.namespace}/{dep.metadata.name}"
        dep_pods = deployment_pods_map.get(key, [])

        print(f"Deployment: {dep.metadata.name}  Namespace: {dep.metadata.namespace}")
        print(
            "Replicas: "
            f"desired={safe_int(dep.spec.replicas if dep.spec else 0)} "
            f"ready={safe_int(dep.status.ready_replicas)} "
            f"updated={safe_int(dep.status.updated_replicas)} "
            f"available={safe_int(dep.status.available_replicas)} "
            f"unavailable={safe_int(dep.status.unavailable_replicas)}"
        )

        if not dep_pods:
            print("(no pods found)\n")
            continue

        pod_rows = []
        for pod in dep_pods:
            pod_rows.append(
                [
                    pod.metadata.name or "-",
                    ready_ratio(pod),
                    "True" if ready_condition(pod) else "False",
                    pod.status.phase or "-",
                    restart_count(pod),
                    pod_reason(pod),
                    pod.spec.node_name or "-",
                    pod.status.pod_ip or "-",
                    age_human(pod.metadata.creation_timestamp) or "-",
                ]
            )

        print_table(
            headers=[
                "POD",
                "CONTAINERS",
                "READY",
                "PHASE",
                "RESTARTS",
                "REASON",
                "NODE",
                "POD_IP",
                "AGE",
            ],
            rows=pod_rows,
        )
        print()


def emit_json(data: dict, compact: bool) -> None:
    if compact:
        print(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def main() -> int:
    args = parse_args()
    config_source = load_cluster_config()

    apps_api = client.AppsV1Api()
    core_api = client.CoreV1Api()

    try:
        if args.namespace:
            deployments = apps_api.list_namespaced_deployment(
                namespace=args.namespace,
                label_selector=args.label_selector,
            ).items
        else:
            deployments = apps_api.list_deployment_for_all_namespaces(
                label_selector=args.label_selector
            ).items
    except ApiException as exc:
        if args.output == "json":
            emit_json(
                {
                    "error": "Failed to list deployments",
                    "details": str(exc),
                },
                compact=args.compact,
            )
        else:
            print(f"Failed to list deployments: {exc}", file=sys.stderr)
        return 2

    router_deployments = [
        dep
        for dep in deployments
        if deployment_looks_like_mysql_router(dep, args.match)
    ]

    if not router_deployments:
        if args.output == "json":
            emit_json(
                {
                    "generated_at": to_iso(datetime.now(timezone.utc)),
                    "config_source": config_source,
                    "scope": {
                        "namespace": args.namespace,
                        "label_selector": args.label_selector,
                        "match": args.match,
                    },
                    "deployment_count": 0,
                    "deployments": [],
                },
                compact=args.compact,
            )
        else:
            scope = f"namespace '{args.namespace}'" if args.namespace else "all namespaces"
            print(f"No MySQL Router deployments found in {scope}.")
        return 0

    try:
        namespaces = [dep.metadata.namespace for dep in router_deployments]
        replica_sets_by_ns, pods_by_ns = build_namespace_cache(
            apps_api, core_api, namespaces
        )
    except ApiException as exc:
        if args.output == "json":
            emit_json(
                {
                    "error": "Failed to list ReplicaSets or Pods",
                    "details": str(exc),
                },
                compact=args.compact,
            )
        else:
            print(f"Failed to list ReplicaSets or Pods: {exc}", file=sys.stderr)
        return 3

    deployment_pods_map: dict[str, list[client.V1Pod]] = {}
    deployments_json = []

    for dep in sorted(
        router_deployments,
        key=lambda item: (item.metadata.namespace or "", item.metadata.name or ""),
    ):
        namespace = dep.metadata.namespace
        dep_pods = pods_for_deployment(
            dep,
            replica_sets_by_ns.get(namespace, []),
            pods_by_ns.get(namespace, []),
        )
        key = f"{dep.metadata.namespace}/{dep.metadata.name}"
        deployment_pods_map[key] = dep_pods
        deployments_json.append(deployment_to_dict(dep, dep_pods))

    if args.output == "json":
        payload = {
            "generated_at": to_iso(datetime.now(timezone.utc)),
            "config_source": config_source,
            "scope": {
                "namespace": args.namespace,
                "label_selector": args.label_selector,
                "match": args.match,
            },
            "deployment_count": len(deployments_json),
            "deployments": deployments_json,
        }
        emit_json(payload, compact=args.compact)
    else:
        render_table(router_deployments, deployment_pods_map)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
