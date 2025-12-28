#!/usr/bin/env python3
"""Build all Docker template images from config.demo.yaml.

This script is called by docker-start.sh during initialization to pre-build
all template images with inline Dockerfile overrides.
"""

import asyncio
import logging
import sys
from pathlib import Path

import yaml

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.config import DockerTemplateConfig
from nexus.core.docker_image_builder import DockerImageBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",  # Simple format for script output
)
logger = logging.getLogger(__name__)


async def main() -> bool:
    """Build all template images with dockerfile_override."""

    # Load config
    config_path = Path(__file__).parent.parent / "configs" / "config.demo.yaml"

    if not config_path.exists():
        logger.error(f"❌ Config file not found: {config_path}")
        return False

    logger.info("📋 Loading Docker template configuration...")
    with open(config_path) as f:
        full_config = yaml.safe_load(f)

    docker_config_dict = full_config.get("docker", {})
    docker_config = DockerTemplateConfig(**docker_config_dict)

    # Find templates with dockerfile_override
    templates_to_build = []
    for name, template in docker_config.templates.items():
        if template.dockerfile_override:
            templates_to_build.append((name, template))

    if not templates_to_build:
        logger.info("✅ No templates with dockerfile_override found - skipping template builds")
        return True

    logger.info(f"📦 Found {len(templates_to_build)} template(s) to build:")
    for name, template in templates_to_build:
        logger.info(f"   • {name} → {template.image}")
    logger.info("")

    # Build each template
    builder = DockerImageBuilder()
    success_count = 0

    for name, template in templates_to_build:
        # Templates with dockerfile_override must have image name
        assert template.image is not None, f"Template {name} must have image name"

        logger.info(f"🔨 Building template '{name}' → {template.image}...")

        # Check if image already exists
        if builder.image_exists(template.image):
            logger.info("   ✅ Image already exists, skipping build")
            success_count += 1
            continue

        # Build the image
        result = await builder.build_from_dockerfile(
            dockerfile_override=template.dockerfile_override,
            image_name=template.image,
            context_path=Path(__file__).parent.parent,
        )

        if result["success"]:
            logger.info(f"   ✅ Built successfully (ID: {result['image_id'][:12]})")
            success_count += 1
        else:
            logger.error(f"   ❌ Build failed: {result.get('error')}")
            logger.error("   Build logs:")
            for log in result.get("logs", []):
                logger.error(f"      {log}")
            # Continue building other templates even if one fails

    logger.info("")
    logger.info(f"✅ Template build complete: {success_count}/{len(templates_to_build)} successful")

    return success_count == len(templates_to_build)


if __name__ == "__main__":
    try:
        success = asyncio.run(main())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.info("\n⚠️  Build interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Build failed with error: {e}", exc_info=True)
        sys.exit(1)
