import pytest
from unittest.mock import MagicMock, patch, call
from kubernetes.client.rest import ApiException

import main as op


class TestBuildDeployment:
    def test_returns_valid_deployment_dict(self, base_spec):
        d = op.build_deployment("mymodel", "default", base_spec)
        assert d["kind"] == "Deployment"
        assert d["apiVersion"] == "apps/v1"

    def test_deployment_name_format(self, base_spec):
        d = op.build_deployment("claude", "default", base_spec)
        assert d["metadata"]["name"] == "llm-claude-deployment"

    def test_deployment_image_from_provider(self, base_spec):
        d = op.build_deployment("claude", "default", base_spec)
        container = d["spec"]["template"]["spec"]["containers"][0]
        assert "mock-backend" in container["image"]

    def test_deployment_min_replicas(self, base_spec):
        base_spec["minReplicas"] = 3
        d = op.build_deployment("claude", "default", base_spec)
        assert d["spec"]["replicas"] == 3

    def test_deployment_env_vars_set(self, base_spec):
        d = op.build_deployment("claude", "default", base_spec)
        container = d["spec"]["template"]["spec"]["containers"][0]
        env_names = {e["name"] for e in container["env"]}
        assert "MODEL_NAME" in env_names
        assert "QUEUE_NAME" in env_names
        assert "REDIS_HOST" in env_names

    def test_deployment_queue_name_uses_model(self, base_spec):
        d = op.build_deployment("testmodel", "default", base_spec)
        container = d["spec"]["template"]["spec"]["containers"][0]
        queue_env = next(e for e in container["env"] if e["name"] == "QUEUE_NAME")
        assert queue_env["value"] == "claude-queue"  # uses spec.model

    def test_deployment_labels_include_provider(self, base_spec):
        d = op.build_deployment("claude", "default", base_spec)
        labels = d["metadata"]["labels"]
        assert labels["provider"] == "anthropic"
        assert labels["managed-by"] == "kubeserve-operator"

    def test_deployment_has_liveness_probe(self, base_spec):
        d = op.build_deployment("claude", "default", base_spec)
        container = d["spec"]["template"]["spec"]["containers"][0]
        assert "livenessProbe" in container

    def test_deployment_unknown_provider_uses_default_image(self):
        spec = {"model": "custom", "provider": "unknown_provider", "minReplicas": 1}
        d = op.build_deployment("custom", "default", spec)
        container = d["spec"]["template"]["spec"]["containers"][0]
        assert "mock-backend" in container["image"]


class TestBuildService:
    def test_service_name_format(self, base_spec):
        s = op.build_service("claude", "default", base_spec)
        assert s["metadata"]["name"] == "llm-claude-service"

    def test_service_kind(self, base_spec):
        s = op.build_service("claude", "default", base_spec)
        assert s["kind"] == "Service"

    def test_service_exposes_http_port(self, base_spec):
        s = op.build_service("claude", "default", base_spec)
        port_names = {p["name"] for p in s["spec"]["ports"]}
        assert "http" in port_names

    def test_service_exposes_metrics_port(self, base_spec):
        s = op.build_service("claude", "default", base_spec)
        port_names = {p["name"] for p in s["spec"]["ports"]}
        assert "http-metrics" in port_names

    def test_service_selector_matches_deployment_labels(self, base_spec):
        s = op.build_service("claude", "default", base_spec)
        assert s["spec"]["selector"] == {"app": "llm-claude"}

    def test_service_type_cluster_ip(self, base_spec):
        s = op.build_service("claude", "default", base_spec)
        assert s["spec"]["type"] == "ClusterIP"


class TestBuildScaledObject:
    def test_scaled_object_kind(self, base_spec):
        so = op.build_scaled_object("claude", "default", base_spec)
        assert so["kind"] == "ScaledObject"
        assert so["apiVersion"] == "keda.sh/v1alpha1"

    def test_scaled_object_name_format(self, base_spec):
        so = op.build_scaled_object("claude", "default", base_spec)
        assert so["metadata"]["name"] == "llm-claude-scaledobject"

    def test_scaled_object_target_ref(self, base_spec):
        so = op.build_scaled_object("claude", "default", base_spec)
        assert so["spec"]["scaleTargetRef"]["name"] == "llm-claude-deployment"

    def test_scaled_object_min_max_replicas(self, base_spec):
        base_spec["minReplicas"] = 0
        base_spec["maxReplicas"] = 20
        so = op.build_scaled_object("claude", "default", base_spec)
        assert so["spec"]["minReplicaCount"] == 0
        assert so["spec"]["maxReplicaCount"] == 20

    def test_scaled_object_redis_trigger(self, base_spec):
        so = op.build_scaled_object("claude", "default", base_spec)
        trigger = so["spec"]["triggers"][0]
        assert trigger["type"] == "redis"

    def test_scaled_object_list_length_equals_threshold(self, base_spec):
        base_spec["queueThreshold"] = 15
        so = op.build_scaled_object("claude", "default", base_spec)
        trigger = so["spec"]["triggers"][0]
        assert trigger["metadata"]["listLength"] == "15"

    def test_scaled_object_list_name_uses_model(self, base_spec):
        so = op.build_scaled_object("claude", "default", base_spec)
        trigger = so["spec"]["triggers"][0]
        assert trigger["metadata"]["listName"] == "claude-queue"


class TestCreateHandler:
    def test_create_calls_all_three_apis(self, base_spec, mock_apps_api, mock_core_api, mock_custom_api):
        spec = MagicMock()
        spec.get = base_spec.get
        spec.__contains__ = lambda self, k: k in base_spec
        spec.items = base_spec.items

        with patch("kopf.adopt"):
            op.create_llm_deployment(
                spec=base_spec,
                name="claude",
                namespace="default",
                logger=MagicMock(),
            )

        mock_apps_api.create_namespaced_deployment.assert_called_once()
        mock_core_api.create_namespaced_service.assert_called_once()
        mock_custom_api.create_namespaced_custom_object.assert_called_once()

    def test_create_patches_on_409_conflict(self, base_spec, mock_apps_api, mock_core_api, mock_custom_api):
        conflict = ApiException(status=409)
        mock_apps_api.create_namespaced_deployment.side_effect = conflict

        with patch("kopf.adopt"):
            op.create_llm_deployment(
                spec=base_spec,
                name="claude",
                namespace="default",
                logger=MagicMock(),
            )

        mock_apps_api.patch_namespaced_deployment.assert_called_once()

    def test_create_returns_resource_names(self, base_spec, mock_apps_api, mock_core_api, mock_custom_api):
        with patch("kopf.adopt"):
            result = op.create_llm_deployment(
                spec=base_spec,
                name="claude",
                namespace="default",
                logger=MagicMock(),
            )
        assert result["deployment"] == "llm-claude-deployment"
        assert result["service"] == "llm-claude-service"

    def test_create_raises_permanent_error_on_non_409(self, base_spec, mock_apps_api, mock_core_api, mock_custom_api):
        import kopf as kopf_module
        mock_apps_api.create_namespaced_deployment.side_effect = ApiException(status=403)

        with patch("kopf.adopt"):
            with pytest.raises(kopf_module.PermanentError):
                op.create_llm_deployment(
                    spec=base_spec,
                    name="claude",
                    namespace="default",
                    logger=MagicMock(),
                )


class TestUpdateHandler:
    def test_update_patches_deployment(self, base_spec, mock_apps_api, mock_custom_api):
        with patch("main._core_api"):
            op.update_llm_deployment(
                spec=base_spec,
                name="claude",
                namespace="default",
                logger=MagicMock(),
            )
        mock_apps_api.patch_namespaced_deployment.assert_called_once()

    def test_update_patches_scaled_object(self, base_spec, mock_apps_api, mock_custom_api):
        with patch("main._core_api"):
            op.update_llm_deployment(
                spec=base_spec,
                name="claude",
                namespace="default",
                logger=MagicMock(),
            )
        mock_custom_api.patch_namespaced_custom_object.assert_called_once()

    def test_update_creates_scaled_object_if_missing(self, base_spec, mock_apps_api, mock_custom_api):
        mock_custom_api.patch_namespaced_custom_object.side_effect = ApiException(status=404)

        with patch("main._core_api"):
            op.update_llm_deployment(
                spec=base_spec,
                name="claude",
                namespace="default",
                logger=MagicMock(),
            )
        mock_custom_api.create_namespaced_custom_object.assert_called_once()


class TestGetImage:
    def test_known_provider_returns_image(self):
        img = op.get_image("claude", "anthropic")
        assert img != ""

    def test_unknown_provider_returns_default(self):
        img = op.get_image("custom", "unknown")
        assert img == op.PROVIDER_IMAGES["default"]
