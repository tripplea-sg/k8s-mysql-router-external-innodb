# MySQL Router Kubernetes Manifest Generator

Generate Kubernetes manifests to deploy **MySQL Router** in Kubernetes for an **external MySQL InnoDB Cluster**.

This project is useful when your MySQL InnoDB Cluster runs **outside** Kubernetes, but you want Kubernetes-native access to it through MySQL Router.

## What this generator creates

The script generates a complete Kubernetes manifest containing:

- An optional `Namespace`
- A bootstrap `Secret`
- One headless `Service` per external MySQL node
- One `Endpoints` object per external MySQL node
- A `Deployment` for MySQL Router
- A `Service` exposing MySQL Router inside Kubernetes

## Repository structure

```text
.
├── README.md
├── LICENSE
├── .gitignore
├── requirements.txt
├── scripts/
│   └── generate_mysql_router_manifest.py
├── examples/
│   └── mysql-router-config.example.yaml
├── config/
│   └── router-config.yaml
├── generated/
│   └── mysql-router.yaml
└── .github/
    └── workflows/
        └── ci.yml
```
usage:
```
python scripts/generate_mysql_router_manifest.py \
  -c config/router-config.yaml \
  -o generated/mysql-router.yaml
```
To check the entire namespace:
```
pip install kubernetes
python list_mysql_router_status.py
```
To check in 1 namespace
```
python list_mysql_router_status.py -n mysql-router
```

