ECSctrl - ECS deployment for humans
===

[![tests](https://github.com/wlatanowicz/ecsctrl/actions/workflows/tests.yml/badge.svg)](https://github.com/wlatanowicz/ecsctrl/actions/workflows/tests.yml)
[![pypi](https://img.shields.io/pypi/v/ecsctrl)](https://pypi.org/project/ecsctrl/)


ECSctrl allows you to interact w ECS task definition, service and SSM parameter store APIs with simple, easy to maintain template-driven ymls. It works by generating yml resource description from a template and passing it directly to boto3 function as parameters. You can reference boto3 [documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs.html) for information on expected data structure.

Template engine
---

ECSctrl uses [Jinja2](https://palletsprojects.com/p/jinja/) under the hood. You can use any expression (values, includes, conditions, loops etc.) that is allowed by Jinja. For example common pattern is to keep environment configuration in a common file and include it in multiple task definitions.

Parameter sources
---

Jinja templates are fed with values from multiple sources given as command arguments:

1. env files with key-value pairs ie. `-e production.env` or `--env-file=staging.env`
2. json files ie. `-j terraform-output.json` or `--env-file=infrastructure.json`
3. key-value pairs provided as command arguments ie. `-v env_name=jupiter` or `--var instance_type=small`
4. system environment - turned on/off with `--sys-env`/`--no-sys-env` option; off by default

Authentication
---

ECSctrl uses boto3. Configure your [aws credentials](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#environment-variables) or set your [environment variables](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#environment-variables) as expected by boto3.


Usage examples
===

All command accept parameter sources as described above. All examples below use the same env file:
```
# production.env
env_name=production
desired_count=5
memory_limit=2048
app_version=latest
aws_region=us-west-1
target_group_arn=arn:aws:elasticloadbalancing:us-west-1:123456:targetgroup/web-backend/6b38ca93a923aecf
execution_role_arn=arn:aws:iam::123456:role/ecs_task_execution_role
task_role_arn=arn:aws:iam::123456:role/ecs_task_role
subnets=subnet-0296669bba,subnet-b5815d42f,subnet-9401e7ab
service_security_groups=sg-d2935617e5,sg-bb45c06af
```

Register new ECS task definition.
---

```yaml
# task-defnition.yaml
family: {{ env_name }}-nginx
tags:
  ManagedBy: ECSctrl
  Environment: {{ env_name }}
executionRoleArn: {{ execution_role_arn }}
taskRoleArn: {{ task_role_arn }}
networkMode: awsvpc
cpu: 512
memory: {{ memory_limit }}
containerDefinitions:
  - name: ngninx
    image: nginx:{{ app_version }}
    memoryReservation: 512
    essential: true
    logConfiguration:
      logDriver: awslogs
      options:
        awslogs-group: {{ env_name }}/nginx
        awslogs-region: {{ aws_region }}
        awslogs-stream-prefix: nginx
        awslogs-create-group: "true"
    portMappings:
      - containerPort: 80
        hostPort: 80
    environment:
      - DEBUG=true
    secrets:
      - DATABASE_PASSWORD={{ env_name }}-DATABASE_PASSWORD
      - SESSION_SECRET_KEY={{ env_name }}-SESSION_SECRET_KEY
```

```bash
ecsctrl task-definition register -e production.env task-definition.yaml
```

Additional options:
- `-c <cluster-name>` / `--update-services-in-cluster=<cluster-name>` - updates all existing services which uses previous version of task definition (task definition family must match) in given cluster. Can be added multiple times for multiple clusters
- `-w` / `--wait` - wait for update of all services to finish. Command will fail if at least one of services will fail to update.

Create new ECS service
---

```yaml
# service.yaml
serviceName: nginx
cluster: {{ env_name }}-ecs-cluster
tags:
  ManagedBy: ECSctrl
  Environment: {{ env_name }}
enableECSManagedTags: true
propagateTags: TASK_DEFINITION
desiredCount: {{ desired_count }}
launchType: FARGATE
loadBalancers:
  - targetGroupArn: {{ target_group_arn }}
    containerName: nginx
    containerPort: 80
taskDefinition: {{ env_name }}-nginx
deploymentConfiguration:
  maximumPercent: 200
  minimumHealthyPercent: 50
  deploymentCircuitBreaker:
    enable: true
    rollback: false
schedulingStrategy: REPLICA
deploymentController:
  type: ECS
networkConfiguration:
  awsvpcConfiguration:
    assignPublicIp: DISABLED
    subnets:
{% for subnet in subnets.split(',') %}
      - {{ subnet }}
{% endfor %}
    securityGroups:
{% for sg in service_security_groups.split(',') %}
      - {{ sg }}
{% endfor %}
```

```bash
ecsctrl service create -e production.env service.yaml
```

Additional options:
- `-w` / `--wait` - wait for service to be fully functional. Command will fail if service fails to start.


Update existing ECS service
---

Update command takes the same service definition file as create command. Payload is converted to match AWS API's requirements for service update - some field are renamed and some are removed.

```bash
ecsctrl service update -e production.env service.yaml
```

Additional options:
- `-w` / `--wait` - wait for service to be fully functional. Command will fail if service fails to start.


Create or update ECS service
---

Updates a service or creates a new one if not exists.

```bash
ecsctrl service create-or-update -e production.env service.yaml
```

Additional options:
- `-w` / `--wait` - wait for service to be fully functional. Command will fail if service fails to start or update.


Single command deployment
---

All-in-one - registers task definition and performs create-or-update of a service. Recommended to use in CIs. Takes both: task definition and service yaml file specs.

```bash
ecsctrl service deploy -e production.env task-definition.yaml service.yaml
```

Additional options:
- `-w` / `--wait` - wait for service to be fully functional. Command will fail if service fails to start or update.


Store secrets in SSM parameter store.
---

Secrets are represented in yaml as SSM name and value pairs. They're uploaded to parameter store as `SecureString`s.

```yaml
# secrets.yaml
{{ env_name }}-DATABASE_PASSWORD: 5w55ARXYbM3vUSVH
{{ env_name }}-SESSION_SECRET_KEY: VADGyLJscJsa4FF2
```

```bash
ecsctrl secrets store -e production.env secrets.yaml
```

Dump secrets from SSM parameter store. You can optionally filter params by name using regexp.
---

```bash
ecsctrl secrets dump -e production.env --filter "db_.*" secrets.yaml
```

Secrets are appended to the output file as soon as each one is fetched, so an interrupted dump keeps what was already written. By default the dump aborts on the first parameter that fails to be fetched (e.g. access denied); secrets written before the failure stay in the file. Use `--skip-errors` to skip such parameters instead - each skipped parameter is reported on stderr.

```bash
ecsctrl secrets dump -e production.env --skip-errors secrets.yaml
```

Use `--start-from` to dump only parameters whose names are alphabetically equal to or after the given value (e.g. to resume after a failure). It can be combined with `--filter`.

```bash
ecsctrl secrets dump -e production.env --start-from "/prod/db" secrets.yaml
```

By default only `SecureString` parameters without a description are written as `name: value`; the others include their `Type` and `Description`. Use `--simple` to write every parameter as `name: value`. Note that the type and description are not kept, so storing such a file creates all parameters as `SecureString`s.

```bash
ecsctrl secrets dump -e production.env --simple secrets.yaml
```
