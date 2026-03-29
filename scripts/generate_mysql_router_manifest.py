#!/usr/bin/env python3
"""
Generate Kubernetes YAML to deploy MySQL Router for an external InnoDB Cluster.

The script creates:
- one headless Service + Endpoints object per external MySQL node
- one Secret containing bootstrap credentials
- one Deployment for MySQL Router
- one Service exposing MySQL Router inside Kubernetes

Usage:
  python generate_mysql_router_manifest.py -c router-config.yaml > mysql-router.yaml
  python generate_mysql_router_manifest.py -c router-config.yaml -o mysql-router.yaml

Dependency:
  pip install pyyaml
"""

from __future__ import annotations

import argparse
import ipaddress
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install it with: pip install pyyaml"
    ) from exc


class LiteralStr(str):
    """String subclass rendered by PyYAML as a block scalar."""


class NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _literal_str_representer(dumper: yaml.SafeDumper, data: LiteralStr) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(LiteralStr, _literal_str_representer, Dumper=NoAliasSafeDumper)


class ConfigError(ValueError):
    pass


def positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{field} must be a positive integer")
    return value


def non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value.strip()


def validate_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field} must be true or false")
    return value


def validate_port(value: Any, field: str) -> int:
    port = positive_int(value, field)
    if port > 65535:
        raise ConfigError(f"{field} must be between 1 and 65535")
    return port


def validate_ip(value: Any, field: str) -> str:
    ip_text = non_empty_string(value, field)
    try:
        ipaddress.ip_address(ip_text)
    except ValueError as exc:
        raise ConfigError(f"{field} must be a valid IPv4 or IPv6 address") from exc
    return ip_text


def ensure_mapping(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a mapping/object")
    return value


def ensure_list(value: Any, field: str) -> List[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list")
    return value


def ensure_unique_ports(ports: Dict[str, int], field: str) -> None:
    seen: Dict[int, str] = {}
    for name, port in ports.items():
        if port in seen:
            raise ConfigError(
                f"{field}.{name} duplicates {field}.{seen[port]} with port {port}"
            )
        seen[port] = name


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        raise ConfigError("config file is empty")
    return ensure_mapping(data, "root")


def normalized_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    namespace = raw.get("namespace", "mysql-router")
    namespace = non_empty_string(namespace, "namespace")

    create_namespace = validate_bool(raw.get("create_namespace", True), "create_namespace")

    router = ensure_mapping(raw.get("router", {}), "router")
    bootstrap = ensure_mapping(router.get("bootstrap", {}), "router.bootstrap")
    storage = ensure_mapping(router.get("storage", {}), "router.storage") if router.get("storage") is not None else {}
    probes = ensure_mapping(router.get("probes", {}), "router.probes") if router.get("probes") is not None else {}
    ports_cfg = ensure_mapping(router.get("ports", {}), "router.ports") if router.get("ports") is not None else {}

    nodes_raw = ensure_list(raw.get("nodes"), "nodes")
    if not nodes_raw:
        raise ConfigError("nodes must contain at least one entry")

    node_names: set[str] = set()
    nodes: List[Dict[str, Any]] = []
    for index, item in enumerate(nodes_raw):
        node = ensure_mapping(item, f"nodes[{index}]")
        name = non_empty_string(node.get("name"), f"nodes[{index}].name")
        if name in node_names:
            raise ConfigError(f"duplicate node name: {name}")
        node_names.add(name)
        nodes.append(
            {
                "name": name,
                "ip": validate_ip(node.get("ip"), f"nodes[{index}].ip"),
                "port": validate_port(node.get("port", 3306), f"nodes[{index}].port"),
            }
        )

    router_name = non_empty_string(router.get("name", "mysql-router"), "router.name")
    router_service_name = non_empty_string(
        router.get("service_name", router_name), "router.service_name"
    )
    router_service_type = non_empty_string(
        router.get("service_type", "ClusterIP"), "router.service_type"
    )
    router_image = non_empty_string(
        router.get("image", "container-registry.oracle.com/mysql/community-router"),
        "router.image",
    )
    router_replicas = positive_int(router.get("replicas", 1), "router.replicas")
    secret_name = non_empty_string(
        router.get("secret_name", f"{router_name}-bootstrap"), "router.secret_name"
    )
    bootstrap_dir = non_empty_string(
        router.get("bootstrap_config_dir", "/router"), "router.bootstrap_config_dir"
    )
    expose_x_protocol = validate_bool(
        router.get("expose_x_protocol", False), "router.expose_x_protocol"
    )
    expose_http_api = validate_bool(
        router.get("expose_http_api", False), "router.expose_http_api"
    )
    bind_address = validate_ip(router.get("bind_address", "0.0.0.0"), "router.bind_address")
    http_bind_address = validate_ip(
        router.get("http_bind_address", bind_address), "router.http_bind_address"
    )

    router_ports = {
        "rw": validate_port(ports_cfg.get("rw", 6446), "router.ports.rw"),
        "ro": validate_port(ports_cfg.get("ro", 6447), "router.ports.ro"),
        "x_rw": validate_port(ports_cfg.get("x_rw", 6448), "router.ports.x_rw"),
        "x_ro": validate_port(ports_cfg.get("x_ro", 6449), "router.ports.x_ro"),
        "http": validate_port(ports_cfg.get("http", 8443), "router.ports.http"),
    }
    ensure_unique_ports(router_ports, "router.ports")

    bootstrap_user = non_empty_string(
        bootstrap.get("user"), "router.bootstrap.user"
    )
    bootstrap_password = non_empty_string(
        bootstrap.get("password"), "router.bootstrap.password"
    )
    bootstrap_host = non_empty_string(
        bootstrap.get("host", nodes[0]["name"]), "router.bootstrap.host"
    )
    bootstrap_port = validate_port(
        bootstrap.get("port", nodes[0]["port"]), "router.bootstrap.port"
    )

    raw_extra_args = ensure_list(
        bootstrap.get("extra_args", []), "router.bootstrap.extra_args"
    )
    bootstrap_extra_args = [
        non_empty_string(item, f"router.bootstrap.extra_args[{idx}]")
        for idx, item in enumerate(raw_extra_args)
    ]

    storage_type = non_empty_string(storage.get("type", "emptyDir"), "router.storage.type")
    if storage_type not in {"emptyDir", "pvc"}:
        raise ConfigError("router.storage.type must be 'emptyDir' or 'pvc'")

    pvc_name: Optional[str] = None
    if storage_type == "pvc":
        pvc_name = non_empty_string(storage.get("pvc_name"), "router.storage.pvc_name")

    readiness_initial_delay = positive_int(
        probes.get("readiness_initial_delay_seconds", 10),
        "router.probes.readiness_initial_delay_seconds",
    )
    liveness_initial_delay = positive_int(
        probes.get("liveness_initial_delay_seconds", 30),
        "router.probes.liveness_initial_delay_seconds",
    )
    probe_port = validate_port(
        probes.get("port", router_ports["rw"]),
        "router.probes.port",
    )
    if probe_port not in set(router_ports.values()):
        raise ConfigError("router.probes.port must match one of router.ports values")

    return {
        "namespace": namespace,
        "create_namespace": create_namespace,
        "nodes": nodes,
        "router": {
            "name": router_name,
            "service_name": router_service_name,
            "service_type": router_service_type,
            "image": router_image,
            "replicas": router_replicas,
            "secret_name": secret_name,
            "bootstrap_config_dir": bootstrap_dir,
            "expose_x_protocol": expose_x_protocol,
            "expose_http_api": expose_http_api,
            "bind_address": bind_address,
            "http_bind_address": http_bind_address,
            "ports": router_ports,
            "bootstrap": {
                "user": bootstrap_user,
                "password": bootstrap_password,
                "host": bootstrap_host,
                "port": bootstrap_port,
                "extra_args": bootstrap_extra_args,
            },
            "storage": {
                "type": storage_type,
                "pvc_name": pvc_name,
            },
            "probes": {
                "port": probe_port,
                "readiness_initial_delay_seconds": readiness_initial_delay,
                "liveness_initial_delay_seconds": liveness_initial_delay,
            },
        },
    }


def router_bootstrap_script(cfg: Dict[str, Any]) -> LiteralStr:
    router = cfg["router"]
    bootstrap_dir = router["bootstrap_config_dir"]
    extra_args = " ".join(shlex.quote(arg) for arg in router["bootstrap"]["extra_args"])

    bootstrap_cmd_parts = [
        "mysqlrouter",
        '--bootstrap "${MYSQL_BOOTSTRAP_USER}@${MYSQL_BOOTSTRAP_HOST}:${MYSQL_BOOTSTRAP_PORT}"',
        f"--directory {shlex.quote(bootstrap_dir)}",
    ]
    if extra_args:
        bootstrap_cmd_parts.append(extra_args)

    bootstrap_cmd = " ".join(bootstrap_cmd_parts)
    conf_path = f"{bootstrap_dir}/mysqlrouter.conf"
    tmp_conf_path = f"{conf_path}.tmp"

    script = f"""set -eu
if [ ! -f {shlex.quote(conf_path)} ]; then
  printf '%s\\n' "${{MYSQL_BOOTSTRAP_PASSWORD}}" | {bootstrap_cmd}
fi
awk \\
  -v ROUTER_BIND_ADDRESS={shlex.quote(router['bind_address'])} \\
  -v HTTP_BIND_ADDRESS={shlex.quote(router['http_bind_address'])} \\
  -v RW_PORT={shlex.quote(str(router['ports']['rw']))} \\
  -v RO_PORT={shlex.quote(str(router['ports']['ro']))} \\
  -v X_RW_PORT={shlex.quote(str(router['ports']['x_rw']))} \\
  -v X_RO_PORT={shlex.quote(str(router['ports']['x_ro']))} \\
  -v HTTP_PORT={shlex.quote(str(router['ports']['http']))} '
function flush_http_bind() {{
  if (section_type == "http_server" && !saw_http_bind) {{
    print "bind_address=" HTTP_BIND_ADDRESS
  }}
}}

/^[[]routing:[^]]+[]]$/ {{
  flush_http_bind()
  section_type = "routing"
  routing_key = $0
  sub(/^\\[routing:/, "", routing_key)
  sub(/\\]$/, "", routing_key)
  saw_http_bind = 0
  print
  next
}}

/^[[]http_server[]]$/ {{
  flush_http_bind()
  section_type = "http_server"
  routing_key = ""
  saw_http_bind = 0
  print
  next
}}

/^[[]/ {{
  flush_http_bind()
  section_type = "other"
  routing_key = ""
  saw_http_bind = 0
  print
  next
}}

section_type == "routing" && /^bind_address=/ {{
  print "bind_address=" ROUTER_BIND_ADDRESS
  next
}}

section_type == "routing" && /^bind_port=/ {{
  if (routing_key ~ /_x_rw$/) {{ print "bind_port=" X_RW_PORT; next }}
  if (routing_key ~ /_x_ro$/) {{ print "bind_port=" X_RO_PORT; next }}
  if (routing_key ~ /_rw$/) {{ print "bind_port=" RW_PORT; next }}
  if (routing_key ~ /_ro$/) {{ print "bind_port=" RO_PORT; next }}
}}

section_type == "http_server" && /^port=/ {{
  print "port=" HTTP_PORT
  next
}}

section_type == "http_server" && /^bind_address=/ {{
  print "bind_address=" HTTP_BIND_ADDRESS
  saw_http_bind = 1
  next
}}

{{
  print
}}

END {{
  flush_http_bind()
}}
' {shlex.quote(conf_path)} > {shlex.quote(tmp_conf_path)}
mv {shlex.quote(tmp_conf_path)} {shlex.quote(conf_path)}
exec mysqlrouter -c {shlex.quote(conf_path)}
"""
    return LiteralStr(script)


def node_service(namespace: str, node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": node["name"],
            "namespace": namespace,
        },
        "spec": {
            "clusterIP": "None",
            "ports": [
                {
                    "name": "mysql",
                    "port": node["port"],
                    "targetPort": node["port"],
                }
            ],
        },
    }


def node_endpoints(namespace: str, node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Endpoints",
        "metadata": {
            "name": node["name"],
            "namespace": namespace,
        },
        "subsets": [
            {
                "addresses": [{"ip": node["ip"]}],
                "ports": [{"name": "mysql", "port": node["port"]}],
            }
        ],
    }


def bootstrap_secret(cfg: Dict[str, Any]) -> Dict[str, Any]:
    router = cfg["router"]
    bootstrap = router["bootstrap"]
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": router["secret_name"],
            "namespace": cfg["namespace"],
        },
        "type": "Opaque",
        "stringData": {
            "MYSQL_BOOTSTRAP_USER": bootstrap["user"],
            "MYSQL_BOOTSTRAP_PASSWORD": bootstrap["password"],
            "MYSQL_BOOTSTRAP_HOST": bootstrap["host"],
            "MYSQL_BOOTSTRAP_PORT": str(bootstrap["port"]),
        },
    }


def deployment(cfg: Dict[str, Any]) -> Dict[str, Any]:
    router = cfg["router"]
    container_ports: List[Dict[str, Any]] = [
        {"name": "mysql-rw", "containerPort": router["ports"]["rw"]},
        {"name": "mysql-ro", "containerPort": router["ports"]["ro"]},
    ]
    if router["expose_x_protocol"]:
        container_ports.extend(
            [
                {"name": "mysqlx-rw", "containerPort": router["ports"]["x_rw"]},
                {"name": "mysqlx-ro", "containerPort": router["ports"]["x_ro"]},
            ]
        )
    if router["expose_http_api"]:
        container_ports.append(
            {"name": "http-api", "containerPort": router["ports"]["http"]}
        )

    volume: Dict[str, Any]
    if router["storage"]["type"] == "pvc":
        volume = {
            "name": "router-data",
            "persistentVolumeClaim": {"claimName": router["storage"]["pvc_name"]},
        }
    else:
        volume = {"name": "router-data", "emptyDir": {}}

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": router["name"],
            "namespace": cfg["namespace"],
        },
        "spec": {
            "replicas": router["replicas"],
            "selector": {"matchLabels": {"app": router["name"]}},
            "template": {
                "metadata": {"labels": {"app": router["name"]}},
                "spec": {
                    "containers": [
                        {
                            "name": "mysqlrouter",
                            "image": router["image"],
                            "imagePullPolicy": "IfNotPresent",
                            "envFrom": [
                                {"secretRef": {"name": router["secret_name"]}}
                            ],
                            "command": ["/bin/sh", "-ec", router_bootstrap_script(cfg)],
                            "ports": container_ports,
                            "volumeMounts": [
                                {
                                    "name": "router-data",
                                    "mountPath": router["bootstrap_config_dir"],
                                }
                            ],
                            "readinessProbe": {
                                "tcpSocket": {"port": router["probes"]["port"]},
                                "initialDelaySeconds": router["probes"][
                                    "readiness_initial_delay_seconds"
                                ],
                                "periodSeconds": 5,
                            },
                            "livenessProbe": {
                                "tcpSocket": {"port": router["probes"]["port"]},
                                "initialDelaySeconds": router["probes"][
                                    "liveness_initial_delay_seconds"
                                ],
                                "periodSeconds": 10,
                            },
                        }
                    ],
                    "volumes": [volume],
                },
            },
        },
    }


def router_service(cfg: Dict[str, Any]) -> Dict[str, Any]:
    router = cfg["router"]
    ports: List[Dict[str, Any]] = [
        {
            "name": "mysql-rw",
            "port": router["ports"]["rw"],
            "targetPort": router["ports"]["rw"],
        },
        {
            "name": "mysql-ro",
            "port": router["ports"]["ro"],
            "targetPort": router["ports"]["ro"],
        },
    ]
    if router["expose_x_protocol"]:
        ports.extend(
            [
                {
                    "name": "mysqlx-rw",
                    "port": router["ports"]["x_rw"],
                    "targetPort": router["ports"]["x_rw"],
                },
                {
                    "name": "mysqlx-ro",
                    "port": router["ports"]["x_ro"],
                    "targetPort": router["ports"]["x_ro"],
                },
            ]
        )
    if router["expose_http_api"]:
        ports.append(
            {
                "name": "http-api",
                "port": router["ports"]["http"],
                "targetPort": router["ports"]["http"],
            }
        )

    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": router["service_name"],
            "namespace": cfg["namespace"],
        },
        "spec": {
            "type": router["service_type"],
            "selector": {"app": router["name"]},
            "ports": ports,
        },
    }


def namespace_doc(namespace: str) -> Dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace},
    }


def build_documents(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    if cfg["create_namespace"]:
        docs.append(namespace_doc(cfg["namespace"]))

    docs.append(bootstrap_secret(cfg))
    for node in cfg["nodes"]:
        docs.append(node_service(cfg["namespace"], node))
        docs.append(node_endpoints(cfg["namespace"], node))
    docs.append(deployment(cfg))
    docs.append(router_service(cfg))
    return docs


def dump_yaml(documents: Iterable[Dict[str, Any]]) -> str:
    return yaml.dump_all(
        list(documents),
        Dumper=NoAliasSafeDumper,
        sort_keys=False,
        explicit_start=True,
        default_flow_style=False,
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Kubernetes manifests for MySQL Router + external InnoDB Cluster nodes."
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        type=Path,
        help="Path to the input YAML config file.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optional output file path. Defaults to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = normalized_config(load_config(args.config))
    output = dump_yaml(build_documents(cfg))

    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}")
