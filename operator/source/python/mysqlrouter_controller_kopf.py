#!/usr/bin/env python3
"""
Python translation of the MySQLRouter controller.

Framework: Kopf + official Kubernetes Python client.

What it reconciles for each MySQLRouter custom resource:
- one selector-less Service per external MySQL node
- one manual EndpointSlice per external MySQL node
- one fronting Service for MySQL Router
- one Deployment running mysqlrouter
- .status updates on the MySQLRouter custom resource

Run locally:
    pip install kopf kubernetes pyyaml
    kopf run mysqlrouter_controller_kopf.py --verbose

Run in cluster:
    copy this file into an image and run: kopf run /app/mysqlrouter_controller_kopf.py
"""

from __future__ import annotations

import copy
import ipaddress
import logging
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import kopf
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

GROUP = "router.example.com"
VERSION = "v1alpha1"
PLURAL = "mysqlrouters"
KIND = "MySQLRouter"
API_VERSION = f"{GROUP}/{VERSION}"
MANAGED_BY_LABEL_VALUE = "mysqlrouter-operator.router.example.com"
FIELD_MANAGER = "mysqlrouter-operator"
DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

CORE_API: Optional[client.CoreV1Api] = None
APPS_API: Optional[client.AppsV1Api] = None
DISCOVERY_API: Optional[client.DiscoveryV1Api] = None
CUSTOM_API: Optional[client.CustomObjectsApi] = None


class SpecValidationError(Exception):
    """Raised when the MySQLRouter spec is invalid."""


@kopf.on.startup()
def on_startup(settings: kopf.OperatorSettings, logger: logging.Logger, **_: Any) -> None:
    global CORE_API, APPS_API, DISCOVERY_API, CUSTOM_API

    settings.posting.enabled = False
    settings.watching.server_timeout = 30
    settings.watching.client_timeout = 45

    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration.")
    except ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig.")

    CORE_API = client.CoreV1Api()
    APPS_API = client.AppsV1Api()
    DISCOVERY_API = client.DiscoveryV1Api()
    CUSTOM_API = client.CustomObjectsApi()


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile_on_event(
    spec: Dict[str, Any],
    name: str,
    namespace: str,
    body: Dict[str, Any],
    meta: Dict[str, Any],
    status: Dict[str, Any],
    logger: logging.Logger,
    **_: Any,
) -> None:
    reconcile_mysqlrouter(
        name=name,
        namespace=namespace,
        body=body,
        spec=spec,
        meta=meta,
        current_status=status,
        logger=logger,
    )


@kopf.timer(GROUP, VERSION, PLURAL, interval=300.0, idle=60.0, sharp=True)
def reconcile_periodically(
    spec: Dict[str, Any],
    name: str,
    namespace: str,
    body: Dict[str, Any],
    meta: Dict[str, Any],
    status: Dict[str, Any],
    logger: logging.Logger,
    **_: Any,
) -> None:
    """Periodic self-heal loop to approximate controller-runtime requeues/Owns()."""
    reconcile_mysqlrouter(
        name=name,
        namespace=namespace,
        body=body,
        spec=spec,
        meta=meta,
        current_status=status,
        logger=logger,
    )


@kopf.on.delete(GROUP, VERSION, PLURAL)
def on_delete(name: str, namespace: str, logger: logging.Logger, **_: Any) -> None:
    logger.info("MySQLRouter %s/%s deleted. Owned resources should be garbage-collected.", namespace, name)


# ---------------------------------------------------------------------------
# Reconcile entrypoint
# ---------------------------------------------------------------------------

def reconcile_mysqlrouter(
    *,
    name: str,
    namespace: str,
    body: Dict[str, Any],
    spec: Dict[str, Any],
    meta: Dict[str, Any],
    current_status: Dict[str, Any],
    logger: logging.Logger,
) -> None:
    router = normalize_router(body)

    try:
        validate_mysqlrouter(router)
        bootstrap_node = bootstrap_node_for(router)
    except SpecValidationError as exc:
        logger.error("Invalid MySQLRouter spec for %s/%s: %s", namespace, name, exc)
        desired_status = build_status(
            router=router,
            generation=int(meta.get("generation", 0)),
            deployment=None,
            node_service_names=[],
            endpoint_slice_names=[],
            reconcile_error=str(exc),
            current_status=current_status or {},
        )
        patch_status_if_changed(name=name, namespace=namespace, current_status=current_status or {}, desired_status=desired_status)
        return

    node_service_names: List[str] = []
    endpoint_slice_names: List[str] = []

    try:
        for node in router["spec"]["externalNodes"]:
            reconcile_external_node_service(router, node)
            reconcile_external_node_endpointslice(router, node)
            service_name = external_node_service_name(router["metadata"]["name"], node["name"])
            node_service_names.append(service_name)
            endpoint_slice_names.append(external_node_endpointslice_name(service_name))

        reconcile_router_service(router)
        deployment = reconcile_router_deployment(router, bootstrap_node)

        desired_status = build_status(
            router=router,
            generation=int(meta.get("generation", 0)),
            deployment=deployment,
            node_service_names=node_service_names,
            endpoint_slice_names=endpoint_slice_names,
            reconcile_error=None,
            current_status=current_status or {},
        )
        patch_status_if_changed(name=name, namespace=namespace, current_status=current_status or {}, desired_status=desired_status)
    except ApiException as exc:
        logger.exception("Reconciliation failed for %s/%s", namespace, name)
        desired_status = build_status(
            router=router,
            generation=int(meta.get("generation", 0)),
            deployment=None,
            node_service_names=node_service_names,
            endpoint_slice_names=endpoint_slice_names,
            reconcile_error=f"{exc.status} {exc.reason}".strip(),
            current_status=current_status or {},
        )
        patch_status_if_changed(name=name, namespace=namespace, current_status=current_status or {}, desired_status=desired_status)
        raise kopf.TemporaryError(f"reconcile failed: {exc}", delay=30.0) from exc
    except Exception as exc:
        logger.exception("Unexpected reconciliation failure for %s/%s", namespace, name)
        desired_status = build_status(
            router=router,
            generation=int(meta.get("generation", 0)),
            deployment=None,
            node_service_names=node_service_names,
            endpoint_slice_names=endpoint_slice_names,
            reconcile_error=str(exc),
            current_status=current_status or {},
        )
        patch_status_if_changed(name=name, namespace=namespace, current_status=current_status or {}, desired_status=desired_status)
        raise kopf.TemporaryError(f"unexpected reconcile failure: {exc}", delay=30.0) from exc


# ---------------------------------------------------------------------------
# Spec normalization + validation
# ---------------------------------------------------------------------------

def normalize_router(body: Dict[str, Any]) -> Dict[str, Any]:
    router = copy.deepcopy(body)
    router.setdefault("metadata", {})
    router.setdefault("spec", {})
    spec = router["spec"]

    spec.setdefault("image", "container-registry.oracle.com/mysql/community-router:8.4")
    spec.setdefault("imagePullPolicy", "IfNotPresent")
    spec.setdefault("replicas", 1)
    spec.setdefault("serviceType", "ClusterIP")
    spec.setdefault("serviceAnnotations", {})

    bootstrap = spec.setdefault("bootstrap", {})
    bootstrap.setdefault("nodeName", "")
    bootstrap.setdefault("extraArgs", [])
    bootstrap.setdefault("usernameSecretRef", {})
    bootstrap.setdefault("passwordSecretRef", {})

    routing = spec.setdefault("routing", {})
    routing.setdefault("bindAddress", "0.0.0.0")
    routing.setdefault("basePort", 6446)
    routing.setdefault("exposeXProtocol", True)

    storage = spec.setdefault("storage", {})
    storage.setdefault("type", "EmptyDir")
    storage.setdefault("claimName", "")

    spec.setdefault("externalNodes", [])
    return router



def validate_mysqlrouter(router: Dict[str, Any]) -> None:
    spec = router["spec"]
    external_nodes = spec.get("externalNodes", [])
    if not external_nodes:
        raise SpecValidationError("spec.externalNodes must contain at least one item")

    base_port = int(spec.get("routing", {}).get("basePort", 0))
    if base_port < 1 or base_port > 65532:
        raise SpecValidationError("spec.routing.basePort must be between 1 and 65532")

    storage = spec.get("storage", {})
    if storage.get("type") == "PersistentVolumeClaim" and not storage.get("claimName"):
        raise SpecValidationError(
            "spec.storage.claimName is required when spec.storage.type is PersistentVolumeClaim"
        )

    username_ref = spec.get("bootstrap", {}).get("usernameSecretRef", {})
    if not username_ref.get("name") or not username_ref.get("key"):
        raise SpecValidationError("spec.bootstrap.usernameSecretRef.name and key are required")

    password_ref = spec.get("bootstrap", {}).get("passwordSecretRef", {})
    if not password_ref.get("name") or not password_ref.get("key"):
        raise SpecValidationError("spec.bootstrap.passwordSecretRef.name and key are required")

    seen = set()
    for node in external_nodes:
        node_name = str(node.get("name", ""))
        if not node_name:
            raise SpecValidationError("each external node must have a name")
        if len(node_name) > 63 or not DNS1123_LABEL_RE.fullmatch(node_name):
            raise SpecValidationError(
                f'external node "{node_name}" is not a valid DNS-1123 label'
            )
        if node_name in seen:
            raise SpecValidationError(f'duplicate external node name "{node_name}"')
        seen.add(node_name)

        address = str(node.get("address", ""))
        try:
            ipaddress.ip_address(address)
        except ValueError as exc:
            raise SpecValidationError(
                f'external node "{node_name}" address "{address}" must be an IPv4 or IPv6 address'
            ) from exc

        port = int(node.get("port", 0))
        if port < 1 or port > 65535:
            raise SpecValidationError(
                f'external node "{node_name}" port must be between 1 and 65535'
            )



def bootstrap_node_for(router: Dict[str, Any]) -> Dict[str, Any]:
    bootstrap_name = str(router["spec"].get("bootstrap", {}).get("nodeName", ""))
    external_nodes = router["spec"]["externalNodes"]

    if not bootstrap_name:
        return external_nodes[0]

    for node in external_nodes:
        if node["name"] == bootstrap_name:
            return node

    raise SpecValidationError(
        f'spec.bootstrap.nodeName "{bootstrap_name}" was not found in spec.externalNodes'
    )


# ---------------------------------------------------------------------------
# Resource builders/helpers
# ---------------------------------------------------------------------------

def labels_for_router(router: Dict[str, Any]) -> Dict[str, str]:
    labels = selector_labels_for_router(router)
    labels["app.kubernetes.io/managed-by"] = MANAGED_BY_LABEL_VALUE
    labels["app.kubernetes.io/component"] = "mysql-router"
    return labels



def selector_labels_for_router(router: Dict[str, Any]) -> Dict[str, str]:
    return {
        "app.kubernetes.io/name": "mysql-router",
        "app.kubernetes.io/instance": router["metadata"]["name"],
    }



def owner_references(router: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = router["metadata"]
    return [
        {
            "apiVersion": API_VERSION,
            "kind": KIND,
            "name": metadata["name"],
            "uid": metadata["uid"],
            "controller": True,
            "blockOwnerDeletion": True,
        }
    ]



def external_node_service_name(router_name: str, node_name: str) -> str:
    return f"{router_name}-{node_name}"



def external_node_endpointslice_name(service_name: str) -> str:
    return f"{service_name}-manual"



def router_service_ports(router: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = int(router["spec"]["routing"]["basePort"])
    ports = [
        {"name": "mysql-rw", "port": base, "protocol": "TCP", "targetPort": base},
        {"name": "mysql-ro", "port": base + 1, "protocol": "TCP", "targetPort": base + 1},
    ]
    if bool(router["spec"]["routing"].get("exposeXProtocol", True)):
        ports.extend(
            [
                {"name": "mysqlx-rw", "port": base + 2, "protocol": "TCP", "targetPort": base + 2},
                {"name": "mysqlx-ro", "port": base + 3, "protocol": "TCP", "targetPort": base + 3},
            ]
        )
    return ports



def router_container_ports(router: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = int(router["spec"]["routing"]["basePort"])
    return [
        {"name": "mysql-rw", "containerPort": base, "protocol": "TCP"},
        {"name": "mysql-ro", "containerPort": base + 1, "protocol": "TCP"},
        {"name": "mysqlx-rw", "containerPort": base + 2, "protocol": "TCP"},
        {"name": "mysqlx-ro", "containerPort": base + 3, "protocol": "TCP"},
    ]



def bootstrap_script(router: Dict[str, Any]) -> str:
    extra_args = [shlex.quote(str(arg)) for arg in router["spec"]["bootstrap"].get("extraArgs", [])]
    base_args = [
        "mysqlrouter",
        '--bootstrap "${MYSQL_BOOTSTRAP_USER}@${MYSQL_BOOTSTRAP_HOST}:${MYSQL_BOOTSTRAP_PORT}"',
        "--directory /router",
        '--conf-base-port "${MYSQL_ROUTER_BASE_PORT}"',
        '--conf-bind-address "${MYSQL_ROUTER_BIND_ADDRESS}"',
        *extra_args,
    ]
    return "\n".join(
        [
            "set -eu",
            "mkdir -p /router",
            "if [ ! -f /router/mysqlrouter.conf ]; then",
            f"  printf '%s\\n' \"${{MYSQL_BOOTSTRAP_PASSWORD}}\" | {' '.join(base_args)}",
            "fi",
            "exec mysqlrouter -c /router/mysqlrouter.conf",
        ]
    )



def storage_volume(router: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    storage = router["spec"]["storage"]
    volume: Dict[str, Any] = {"name": "router-data"}
    if storage.get("type") == "PersistentVolumeClaim":
        volume["persistentVolumeClaim"] = {"claimName": storage["claimName"]}
    else:
        volume["emptyDir"] = {}
    return volume, {"name": "router-data", "mountPath": "/router"}



def secret_env_var(name: str, ref: Dict[str, str]) -> Dict[str, Any]:
    return {
        "name": name,
        "valueFrom": {
            "secretKeyRef": {
                "name": ref["name"],
                "key": ref["key"],
            }
        },
    }



def ssa_kwargs() -> Dict[str, Any]:
    return {
        "field_manager": FIELD_MANAGER,
        "force": True,
        "_content_type": "application/apply-patch+yaml",
    }


# ---------------------------------------------------------------------------
# Resource reconciliation
# ---------------------------------------------------------------------------

def reconcile_external_node_service(router: Dict[str, Any], node: Dict[str, Any]) -> None:
    core_api = require_core_api()
    namespace = router["metadata"]["namespace"]
    name = external_node_service_name(router["metadata"]["name"], node["name"])
    labels = labels_for_router(router)
    labels["mysqlrouter.router.example.com/external-node"] = node["name"]

    body = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "ownerReferences": owner_references(router),
        },
        "spec": {
            "type": "ClusterIP",
            "ports": [
                {
                    "name": "mysql",
                    "port": int(node["port"]),
                    "protocol": "TCP",
                }
            ],
        },
    }
    core_api.patch_namespaced_service(name=name, namespace=namespace, body=body, **ssa_kwargs())



def reconcile_external_node_endpointslice(router: Dict[str, Any], node: Dict[str, Any]) -> None:
    discovery_api = require_discovery_api()
    namespace = router["metadata"]["namespace"]
    service_name = external_node_service_name(router["metadata"]["name"], node["name"])
    name = external_node_endpointslice_name(service_name)
    labels = labels_for_router(router)
    labels["kubernetes.io/service-name"] = service_name
    labels["endpointslice.kubernetes.io/managed-by"] = MANAGED_BY_LABEL_VALUE
    labels["mysqlrouter.router.example.com/external-node"] = node["name"]

    body = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "ownerReferences": owner_references(router),
        },
        "addressType": "IPv6" if ipaddress.ip_address(node["address"]).version == 6 else "IPv4",
        "ports": [
            {
                "name": "mysql",
                "port": int(node["port"]),
                "protocol": "TCP",
            }
        ],
        "endpoints": [
            {
                "addresses": [node["address"]],
                "conditions": {"ready": True},
            }
        ],
    }
    discovery_api.patch_namespaced_endpoint_slice(name=name, namespace=namespace, body=body, **ssa_kwargs())



def reconcile_router_service(router: Dict[str, Any]) -> None:
    core_api = require_core_api()
    namespace = router["metadata"]["namespace"]
    name = router["metadata"]["name"]

    body = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels_for_router(router),
            "annotations": router["spec"].get("serviceAnnotations", {}),
            "ownerReferences": owner_references(router),
        },
        "spec": {
            "type": router["spec"].get("serviceType", "ClusterIP"),
            "selector": selector_labels_for_router(router),
            "ports": router_service_ports(router),
        },
    }
    core_api.patch_namespaced_service(name=name, namespace=namespace, body=body, **ssa_kwargs())



def reconcile_router_deployment(router: Dict[str, Any], bootstrap_node: Dict[str, Any]) -> client.V1Deployment:
    apps_api = require_apps_api()
    namespace = router["metadata"]["namespace"]
    name = router["metadata"]["name"]
    volume, volume_mount = storage_volume(router)
    base_port = int(router["spec"]["routing"]["basePort"])

    body = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels_for_router(router),
            "ownerReferences": owner_references(router),
        },
        "spec": {
            "replicas": int(router["spec"].get("replicas", 1)),
            "selector": {"matchLabels": selector_labels_for_router(router)},
            "template": {
                "metadata": {"labels": selector_labels_for_router(router)},
                "spec": {
                    "volumes": [volume],
                    "containers": [
                        {
                            "name": "mysqlrouter",
                            "image": router["spec"]["image"],
                            "imagePullPolicy": router["spec"].get("imagePullPolicy", "IfNotPresent"),
                            "command": ["/bin/sh", "-ec", bootstrap_script(router)],
                            "env": [
                                secret_env_var("MYSQL_BOOTSTRAP_USER", router["spec"]["bootstrap"]["usernameSecretRef"]),
                                secret_env_var("MYSQL_BOOTSTRAP_PASSWORD", router["spec"]["bootstrap"]["passwordSecretRef"]),
                                {
                                    "name": "MYSQL_BOOTSTRAP_HOST",
                                    "value": external_node_service_name(router["metadata"]["name"], bootstrap_node["name"]),
                                },
                                {"name": "MYSQL_BOOTSTRAP_PORT", "value": str(int(bootstrap_node["port"]))},
                                {"name": "MYSQL_ROUTER_BASE_PORT", "value": str(base_port)},
                                {
                                    "name": "MYSQL_ROUTER_BIND_ADDRESS",
                                    "value": router["spec"]["routing"].get("bindAddress", "0.0.0.0"),
                                },
                            ],
                            "ports": router_container_ports(router),
                            "volumeMounts": [volume_mount],
                            "readinessProbe": {
                                "tcpSocket": {"port": base_port},
                                "initialDelaySeconds": 10,
                                "periodSeconds": 5,
                            },
                            "livenessProbe": {
                                "tcpSocket": {"port": base_port},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 10,
                            },
                        }
                    ],
                },
            },
        },
    }

    apps_api.patch_namespaced_deployment(name=name, namespace=namespace, body=body, **ssa_kwargs())
    return apps_api.read_namespaced_deployment(name=name, namespace=namespace)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def build_status(
    *,
    router: Dict[str, Any],
    generation: int,
    deployment: Optional[client.V1Deployment],
    node_service_names: List[str],
    endpoint_slice_names: List[str],
    reconcile_error: Optional[str],
    current_status: Dict[str, Any],
) -> Dict[str, Any]:
    desired_replicas = int(router["spec"].get("replicas", 1))

    if deployment is not None:
        ready_replicas = safe_int(getattr(deployment.status, "ready_replicas", 0))
        phase = derive_phase(deployment)
    elif reconcile_error:
        ready_replicas = safe_int(current_status.get("readyReplicas", 0))
        phase = "Error"
    else:
        ready_replicas = safe_int(current_status.get("readyReplicas", 0))
        phase = "Pending"

    conditions = copy.deepcopy(current_status.get("conditions", []))
    if reconcile_error:
        conditions = set_status_condition(
            conditions,
            type_="Ready",
            status="False",
            reason="ReconcileError",
            message=reconcile_error,
            observed_generation=generation,
        )
        conditions = set_status_condition(
            conditions,
            type_="Reconciled",
            status="False",
            reason="ReconcileError",
            message=reconcile_error,
            observed_generation=generation,
        )
    else:
        ready = deployment is not None and ready_replicas >= desired_replicas
        conditions = set_status_condition(
            conditions,
            type_="Ready",
            status="True" if ready else "False",
            reason="Ready" if ready else "Progressing",
            message="Deployment is ready" if ready else "Deployment is reconciling",
            observed_generation=generation,
        )
        conditions = set_status_condition(
            conditions,
            type_="Reconciled",
            status="True",
            reason="Success",
            message="Resources are in sync with spec",
            observed_generation=generation,
        )

    return {
        "observedGeneration": generation,
        "phase": phase,
        "readyReplicas": ready_replicas,
        "deploymentName": router["metadata"]["name"],
        "serviceName": router["metadata"]["name"],
        "nodeServiceNames": sorted(node_service_names),
        "endpointSliceNames": sorted(endpoint_slice_names),
        "conditions": conditions,
    }



def patch_status_if_changed(*, name: str, namespace: str, current_status: Dict[str, Any], desired_status: Dict[str, Any]) -> None:
    custom_api = require_custom_api()
    if (current_status or {}) == desired_status:
        return
    custom_api.patch_namespaced_custom_object_status(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=PLURAL,
        name=name,
        body={"status": desired_status},
    )



def set_status_condition(
    conditions: List[Dict[str, Any]],
    *,
    type_: str,
    status: str,
    reason: str,
    message: str,
    observed_generation: int,
) -> List[Dict[str, Any]]:
    now = utcnow_rfc3339()
    updated = copy.deepcopy(conditions)

    for condition in updated:
        if condition.get("type") != type_:
            continue
        unchanged = (
            condition.get("status") == status
            and condition.get("reason") == reason
            and condition.get("message") == message
            and safe_int(condition.get("observedGeneration", 0)) == safe_int(observed_generation)
        )
        condition["status"] = status
        condition["reason"] = reason
        condition["message"] = message
        condition["observedGeneration"] = observed_generation
        if not unchanged:
            condition["lastTransitionTime"] = now
        condition.setdefault("lastTransitionTime", now)
        return updated

    updated.append(
        {
            "type": type_,
            "status": status,
            "reason": reason,
            "message": message,
            "observedGeneration": observed_generation,
            "lastTransitionTime": now,
        }
    )
    return updated



def derive_phase(deployment: client.V1Deployment) -> str:
    desired = deployment.spec.replicas if deployment.spec and deployment.spec.replicas is not None else 1
    ready = safe_int(getattr(deployment.status, "ready_replicas", 0))
    updated = safe_int(getattr(deployment.status, "updated_replicas", 0))
    available = safe_int(getattr(deployment.status, "available_replicas", 0))

    if desired > 0 and ready >= desired:
        return "Ready"
    if updated > 0 or available > 0:
        return "Progressing"
    return "Pending"



def safe_int(value: Any) -> int:
    return int(value or 0)



def utcnow_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Lazy API getters
# ---------------------------------------------------------------------------

def require_core_api() -> client.CoreV1Api:
    if CORE_API is None:
        raise RuntimeError("CoreV1Api is not initialized")
    return CORE_API



def require_apps_api() -> client.AppsV1Api:
    if APPS_API is None:
        raise RuntimeError("AppsV1Api is not initialized")
    return APPS_API



def require_discovery_api() -> client.DiscoveryV1Api:
    if DISCOVERY_API is None:
        raise RuntimeError("DiscoveryV1Api is not initialized")
    return DISCOVERY_API



def require_custom_api() -> client.CustomObjectsApi:
    if CUSTOM_API is None:
        raise RuntimeError("CustomObjectsApi is not initialized")
    return CUSTOM_API
