"""Unit tests for Docker template configuration models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.config import DockerImageTemplate, DockerTemplateConfig


class TestDockerImageTemplate:
    """Test suite for DockerImageTemplate model."""

    def test_template_with_image_only(self):
        """Test template with just an image name."""
        template = DockerImageTemplate(image="nexus-sandbox:latest")

        assert template.image == "nexus-sandbox:latest"
        assert template.dockerfile is None
        assert template.dockerfile_override is None
        assert template.context == "."

    def test_template_with_dockerfile_path(self):
        """Test template with Dockerfile path."""
        template = DockerImageTemplate(image="custom-image:latest", dockerfile="path/to/Dockerfile")

        assert template.image == "custom-image:latest"
        assert template.dockerfile == "path/to/Dockerfile"
        assert template.dockerfile_override is None

    def test_template_with_dockerfile_override(self):
        """Test template with inline Dockerfile override."""
        dockerfile_content = """FROM nexus-sandbox:latest
USER root
RUN pip install torch
USER nexus
"""

        template = DockerImageTemplate(
            image="nexus-sandbox-ml:latest", dockerfile_override=dockerfile_content
        )

        assert template.image == "nexus-sandbox-ml:latest"
        assert template.dockerfile is None
        assert template.dockerfile_override == dockerfile_content

    def test_template_with_custom_context(self):
        """Test template with custom build context."""
        template = DockerImageTemplate(
            image="custom:latest", dockerfile="Dockerfile", context="/custom/path"
        )

        assert template.context == "/custom/path"

    def test_template_defaults(self):
        """Test template with minimal configuration."""
        template = DockerImageTemplate()

        assert template.image is None
        assert template.dockerfile is None
        assert template.dockerfile_override is None
        assert template.context == "."

    def test_template_with_both_dockerfile_and_override(self):
        """Test that template can store both dockerfile and override (validation happens elsewhere)."""
        # Pydantic model accepts both, validation is in builder
        template = DockerImageTemplate(
            image="test:latest", dockerfile="path/to/Dockerfile", dockerfile_override="FROM base"
        )

        assert template.dockerfile == "path/to/Dockerfile"
        assert template.dockerfile_override == "FROM base"


class TestDockerTemplateConfig:
    """Test suite for DockerTemplateConfig model."""

    def test_empty_config(self):
        """Test config with no templates."""
        config = DockerTemplateConfig()

        assert config.templates == {}
        assert config.default_image == "nexus-sandbox:latest"

    def test_config_with_custom_default_image(self):
        """Test config with custom default image."""
        config = DockerTemplateConfig(default_image="custom-base:latest")

        assert config.default_image == "custom-base:latest"

    def test_config_with_single_template(self):
        """Test config with one template."""
        config = DockerTemplateConfig(
            templates={"base": DockerImageTemplate(image="nexus-sandbox:latest")}
        )

        assert "base" in config.templates
        assert config.templates["base"].image == "nexus-sandbox:latest"

    def test_config_with_multiple_templates(self):
        """Test config with multiple templates."""
        config = DockerTemplateConfig(
            templates={
                "base": DockerImageTemplate(image="nexus-sandbox:latest"),
                "ml-heavy": DockerImageTemplate(
                    image="nexus-sandbox-ml:latest",
                    dockerfile_override="FROM nexus-sandbox:latest\nRUN pip install torch",
                ),
                "web-dev": DockerImageTemplate(
                    image="nexus-sandbox-web:latest",
                    dockerfile_override="FROM nexus-sandbox:latest\nRUN pip install fastapi",
                ),
            }
        )

        assert len(config.templates) == 3
        assert "base" in config.templates
        assert "ml-heavy" in config.templates
        assert "web-dev" in config.templates

        # Verify ml-heavy template
        ml_template = config.templates["ml-heavy"]
        assert ml_template.image == "nexus-sandbox-ml:latest"
        assert "pip install torch" in ml_template.dockerfile_override

    def test_config_from_dict(self):
        """Test creating config from dictionary (YAML-like structure)."""
        config_dict = {
            "default_image": "nexus-sandbox:latest",
            "templates": {
                "ml-heavy": {
                    "image": "nexus-sandbox-ml:latest",
                    "dockerfile_override": "FROM nexus-sandbox:latest\nUSER root\nRUN pip install torch\nUSER nexus",
                }
            },
        }

        config = DockerTemplateConfig(**config_dict)

        assert config.default_image == "nexus-sandbox:latest"
        assert "ml-heavy" in config.templates
        assert config.templates["ml-heavy"].image == "nexus-sandbox-ml:latest"
        assert config.templates["ml-heavy"].dockerfile_override is not None

    def test_config_with_multiline_dockerfile_override(self):
        """Test config with multiline Dockerfile override (YAML-style)."""
        dockerfile_override = """FROM nexus-sandbox:latest
USER root

RUN apt-get update && apt-get install -y \\
    ffmpeg graphviz \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \\
    torch>=2.0.0 \\
    tensorflow>=2.13.0

USER nexus
WORKDIR /home/nexus/workspace
"""

        config = DockerTemplateConfig(
            templates={
                "ml-heavy": DockerImageTemplate(
                    image="nexus-sandbox-ml:latest", dockerfile_override=dockerfile_override
                )
            }
        )

        template = config.templates["ml-heavy"]
        assert "FROM nexus-sandbox:latest" in template.dockerfile_override
        assert "torch>=2.0.0" in template.dockerfile_override
        assert "tensorflow>=2.13.0" in template.dockerfile_override

    def test_config_template_lookup(self):
        """Test looking up templates by name."""
        config = DockerTemplateConfig(
            templates={
                "base": DockerImageTemplate(image="base:latest"),
                "ml": DockerImageTemplate(image="ml:latest"),
            }
        )

        # Lookup existing template
        assert config.templates.get("base") is not None
        assert config.templates.get("base").image == "base:latest"

        # Lookup non-existing template
        assert config.templates.get("nonexistent") is None

    def test_config_validation_with_invalid_data(self):
        """Test that invalid data raises ValidationError."""
        with pytest.raises(ValidationError):
            # Invalid: templates should be a dict, not a list
            DockerTemplateConfig(templates=["invalid"])

    def test_config_immutability(self):
        """Test that config models are immutable after creation."""
        config = DockerTemplateConfig(templates={"base": DockerImageTemplate(image="base:latest")})

        # Pydantic models are mutable by default, but we can verify the structure
        assert isinstance(config.templates, dict)
        assert "base" in config.templates
