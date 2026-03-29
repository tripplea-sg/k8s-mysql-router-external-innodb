#!/usr/bin/env python3
"""
List MySQL Router deployments and the status of their pods.

Examples:
  python list_mysql_router_status.py
  python list_mysql_router_status.py -n mysql-router
  python list_mysql_router_status.py --label-selector app=mysql-router
  python list_mysql_router_status.py --match mysql-router
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.config.config_exception import ConfigException


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List MySQL Router deployments and the status of their pods."
    )
    parser.add_argument(
        "-n",
        "--namespace",
        help="Namespace to search. If omitted, search all namespaces.",
    )
    parser.add_argument(
        "--label-selector",
        help="Optional label selector to narrow the deployments returned by the API.",
    )
    parser.add_argument(
        "--match",
        default="mysql-router",
        help=(
            "Case-insensitive text used to identify MySQL Router deployments "
            "(default: %(default)s)."
        ),
    )
    return parser.parse_args()


def load_cluster_config() -> None:
    try:
        config.load_kube_config()
        return
    except ConfigException:
        pass

    try:
        config.load_incluster_config()
    except ConfigException as exc:
        raise SystemExit(
            "Could not load Kubernetes configuration. "
            "Tried local kubeconfig and in-cluster config."
        ) from exc


def safe_int(value: Optional[int]) -> int:
    return int(value or 0)


def format_age(created_at) -> str:
    if not created_at:
        return "-"
    now = datetime.now(timezone.utc)
    delta = now - created_at
    seconds = max(0, int(delta.total_seconds()))

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


def ready_ratio(pod: client.V1Pod) -> str:
    statuses = pod.status.container_statuses or []
    total = len(pod.spec.containers or [])
    ready = sum(1 for status in statuses if status.ready)
    return f"{ready}/{total}" if total else "0/0"


def restart_count(pod: client.V1Pod) -> int:
    total = 0
    for status in (pod.status.init_container_statuses or []):
        total += int(status.restart_count or 0)
    for status in (pod.status.container_statuses or []):
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
    deployment: client.V1Deployment, match_text: Optional[str]
) -> bool:
    haystack_parts: List[str] = [deployment.metadata.name or ""]

    for mapping in (
        deployment.metadata.labels or {},
        deployment.spec.selector.match_labels or {},
        deployment.spec.template.metadata.labels or {},
    ):
        for key, value in mapping.items():
            haystack_parts.append(str(key))
            haystack_parts.append(str(value))

    for container_spec in deployment.spec.template.spec.containers or []:
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

    return sorted(matched_pods, key=lambda pod: pod.metadata.name or "")


def main() -> int:
    args = parse_args()
    load_cluster_config()

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
        print(f"Failed to list deployments: {exc}", file=sys.stderr)
        return 2

    router_deployments = [
        dep
        for dep in deployments
        if deployment_looks_like_mysql_router(dep, args.match)
    ]

    if not router_deployments:
        scope = f"namespace '{args.namespace}'" if args.namespace else "all namespaces"
        print(f"No MySQL Router deployments found in {scope}.")
        return 0

    namespaces = [dep.metadata.namespace for dep in router_deployments]
    try:
        replica_sets_by_ns, pods_by_ns = build_namespace_cache(
            apps_api, core_api, namespaces
        )
    except ApiException as exc:
        print(f"Failed to list ReplicaSets or Pods: {exc}", file=sys.stderr)
        return 3

    summary_rows = []
    for dep in sorted(
        router_deployments,
        key=lambda item: (item.metadata.namespace or "", item.metadata.name or ""),
    ):
        ns = dep.metadata.namespace
        dep_pods = pods_for_deployment(
            dep,
            replica_sets_by_ns.get(ns, []),
            pods_by_ns.get(ns, []),
        )

        summary_rows.append(
            [
                ns,
                dep.metadata.name or "-",
                safe_int(dep.spec.replicas),
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
        router_deployments,
        key=lambda item: (item.metadata.namespace or "", item.metadata.name or ""),
    ):
        ns = dep.metadata.namespace
        dep_pods = pods_for_deployment(
            dep,
            replica_sets_by_ns.get(ns, []),
            pods_by_ns.get(ns, []),
        )

        print(f"Deployment: {dep.metadata.name}  Namespace: {ns}")
        print(
            "Replicas: "
            f"desired={safe_int(dep.spec.replicas)} "
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
                    format_age(pod.metadata.creation_timestamp),
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
