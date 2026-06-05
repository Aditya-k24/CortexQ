"""
KubeServe Operator — watches LLMDeployment CRDs and reconciles
Kubernetes Deployments, Services, and KEDA ScaledObjects.
"""
import logging
import os
from typing import Any, Dict

import kopf
import kubernetes
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

logger = logging.getLogger("kubeserve.operator")

REDIS_HOST = os.getenv("REDIS_HOST", "redis-master")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
OPERATOR_IMAGE_PREFIX = os.getenv("OPERATOR_IMAGE_PREFIX", "kubeserve")

PROVIDER_IMAGES: Dict[str, str] = {
    "anthropic": f"{OPERATOR_IMAGE_PREFIX}/mock-backend:latest",
    "openai": f"{OPERATOR_IMAGE_PREFIX}/mock-backend:latest",
    "google": f"{OPERATOR_IMAGE_PREFIX}/mock-backend:latest",
    "default": f"{OPERATOR_IMAGE_PREFIX}/mock-backend:latest",
}


def get_image(model: str, provider: str) -> str:
    return PROVIDER_IMAGES.get(provider, PROVIDER_IMAGES["default"])


def build_deployment(name: str, namespace: str, spec: dict) -> dict:
    model = spec.get("model", name)
    provider = spec.get("provider", "default")
    min_replicas = spec.get("minReplicas", 1)
    image = get_image(model, provider)
    queue_name = f"{model}-queue"

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": f"llm-{name}-deployment",
            "namespace": namespace,
            "labels": {"app": f"llm-{name}", "provider": provider, "managed-by": "kubeserve-operator"},
        },
        "spec": {
            "replicas": min_replicas,
            "selector": {"matchLabels": {"app": f"llm-{name}"}},
            "template": {
                "metadata": {"labels": {"app": f"llm-{name}", "provider": provider}},
                "spec": {
                    "containers": [
                        {
                            "name": f"llm-{name}",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"containerPort": 8000, "name": "http"}],
                            "env": [
                                {"name": "MODEL_NAME", "value": model},
                                {"name": "PROVIDER", "value": provider},
                                {"name": "REDIS_HOST", "value": REDIS_HOST},
                                {"name": "REDIS_PORT", "value": REDIS_PORT},
                                {"name": "QUEUE_NAME", "value": queue_name},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/health", "port": 8000},
                                "initialDelaySeconds": 10,
                                "periodSeconds": 15,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 8000},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                            },
                        }
                    ]
                },
            },
        },
    }


def build_service(name: str, namespace: str, spec: dict) -> dict:
    provider = spec.get("provider", "default")
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"llm-{name}-service",
            "namespace": namespace,
            "labels": {"app": f"llm-{name}", "provider": provider, "managed-by": "kubeserve-operator"},
        },
        "spec": {
            "selector": {"app": f"llm-{name}"},
            "ports": [
                {"port": 80, "targetPort": 8000, "protocol": "TCP", "name": "http"},
                {"port": 9090, "targetPort": 9090, "protocol": "TCP", "name": "http-metrics"},
            ],
            "type": "ClusterIP",
        },
    }


def build_scaled_object(name: str, namespace: str, spec: dict) -> dict:
    model = spec.get("model", name)
    min_replicas = spec.get("minReplicas", 0)
    max_replicas = spec.get("maxReplicas", 10)
    queue_threshold = spec.get("queueThreshold", 5)

    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {
            "name": f"llm-{name}-scaledobject",
            "namespace": namespace,
            "labels": {"managed-by": "kubeserve-operator"},
        },
        "spec": {
            "scaleTargetRef": {"name": f"llm-{name}-deployment"},
            "minReplicaCount": min_replicas,
            "maxReplicaCount": max_replicas,
            "cooldownPeriod": 300,
            "triggers": [
                {
                    "type": "redis",
                    "metadata": {
                        "address": f"{REDIS_HOST}.{namespace}.svc.cluster.local:{REDIS_PORT}",
                        "listName": f"{model}-queue",
                        "listLength": str(queue_threshold),
                    },
                }
            ],
        },
    }


def _apps_api() -> k8s_client.AppsV1Api:
    return k8s_client.AppsV1Api()


def _core_api() -> k8s_client.CoreV1Api:
    return k8s_client.CoreV1Api()


def _custom_api() -> k8s_client.CustomObjectsApi:
    return k8s_client.CustomObjectsApi()


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **kwargs):
    settings.persistence.finalizer = "kubeserve.io/finalizer"
    try:
        kubernetes.config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
        logger.info("Loaded local kubeconfig")


@kopf.on.create("kubeserve.io", "v1alpha1", "llmdeployments")
def create_llm_deployment(spec: kopf.Spec, name: str, namespace: str, logger: logging.Logger, **kwargs):
    logger.info("Creating LLMDeployment: %s/%s", namespace, name)

    deployment = build_deployment(name, namespace, dict(spec))
    service = build_service(name, namespace, dict(spec))
    scaled_object = build_scaled_object(name, namespace, dict(spec))

    kopf.adopt(deployment)
    kopf.adopt(service)
    kopf.adopt(scaled_object)

    apps = _apps_api()
    core = _core_api()
    custom = _custom_api()

    try:
        apps.create_namespaced_deployment(namespace, deployment)
        logger.info("Created Deployment llm-%s-deployment", name)
    except ApiException as e:
        if e.status == 409:
            apps.patch_namespaced_deployment(f"llm-{name}-deployment", namespace, deployment)
            logger.info("Patched existing Deployment llm-%s-deployment", name)
        else:
            raise kopf.PermanentError(f"Failed to create Deployment: {e}") from e

    try:
        core.create_namespaced_service(namespace, service)
        logger.info("Created Service llm-%s-service", name)
    except ApiException as e:
        if e.status == 409:
            core.patch_namespaced_service(f"llm-{name}-service", namespace, service)
        else:
            raise kopf.PermanentError(f"Failed to create Service: {e}") from e

    try:
        custom.create_namespaced_custom_object(
            group="keda.sh", version="v1alpha1", namespace=namespace,
            plural="scaledobjects", body=scaled_object,
        )
        logger.info("Created ScaledObject llm-%s-scaledobject", name)
    except ApiException as e:
        if e.status == 409:
            custom.patch_namespaced_custom_object(
                group="keda.sh", version="v1alpha1", namespace=namespace,
                plural="scaledobjects", name=f"llm-{name}-scaledobject", body=scaled_object,
            )
        else:
            raise kopf.PermanentError(f"Failed to create ScaledObject: {e}") from e

    return {"deployment": f"llm-{name}-deployment", "service": f"llm-{name}-service"}


@kopf.on.update("kubeserve.io", "v1alpha1", "llmdeployments")
def update_llm_deployment(spec: kopf.Spec, name: str, namespace: str, logger: logging.Logger, **kwargs):
    logger.info("Updating LLMDeployment: %s/%s", namespace, name)

    deployment = build_deployment(name, namespace, dict(spec))
    scaled_object = build_scaled_object(name, namespace, dict(spec))

    apps = _apps_api()
    custom = _custom_api()

    apps.patch_namespaced_deployment(f"llm-{name}-deployment", namespace, deployment)
    logger.info("Patched Deployment llm-%s-deployment", name)

    try:
        custom.patch_namespaced_custom_object(
            group="keda.sh", version="v1alpha1", namespace=namespace,
            plural="scaledobjects", name=f"llm-{name}-scaledobject", body=scaled_object,
        )
        logger.info("Patched ScaledObject llm-%s-scaledobject", name)
    except ApiException as e:
        if e.status == 404:
            custom.create_namespaced_custom_object(
                group="keda.sh", version="v1alpha1", namespace=namespace,
                plural="scaledobjects", body=scaled_object,
            )
        else:
            raise


@kopf.on.delete("kubeserve.io", "v1alpha1", "llmdeployments")
def delete_llm_deployment(name: str, namespace: str, logger: logging.Logger, **kwargs):
    logger.info("Deleting LLMDeployment children: %s/%s", namespace, name)
    # Kubernetes garbage-collects owned resources automatically via ownerReferences
    # set by kopf.adopt(). Explicit cleanup is a fallback for non-owned resources.


@kopf.on.field("kubeserve.io", "v1alpha1", "llmdeployments", field="spec.minReplicas")
def scale_min_replicas(spec: kopf.Spec, name: str, namespace: str, logger: logging.Logger, **kwargs):
    min_replicas = spec.get("minReplicas", 1)
    apps = _apps_api()
    patch = {"spec": {"replicas": min_replicas}}
    try:
        apps.patch_namespaced_deployment(f"llm-{name}-deployment", namespace, patch)
        logger.info("Updated minReplicas to %d for %s", min_replicas, name)
    except ApiException as e:
        if e.status != 404:
            raise
