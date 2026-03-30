In bash:
```
pip install -r requirements-operator.txt
kopf run mysqlrouter_controller_kopf.py --verbose
```
For in-cluster use, the controller container command should be:
```
pip install -r requirements-operator.txt
kopf run mysqlrouter_controller_kopf.py --verbose
```
