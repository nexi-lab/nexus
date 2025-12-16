"""Script to zip default skills and save them to data/skills/ directory.

Usage:
    python scripts/zip_default_skills.py
"""

import io
import os
import zipfile
from pathlib import Path


def zip_skill_folder(skill_folder_path: Path, output_path: Path) -> None:
    """Zip a skill folder into a .skill package.

    Args:
        skill_folder_path: Path to the skill folder (e.g., /path/to/skill-creator)
        output_path: Path where the .skill file should be saved
    """
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # Get the skill folder name (last part of path)
        skill_name = skill_folder_path.name

        # Walk through all files in the skill folder
        for root, dirs, files in os.walk(skill_folder_path):
            # Skip hidden directories like .git, __pycache__, etc.
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]

            for file in files:
                # Skip hidden files and common build artifacts
                if file.startswith(".") or file.endswith(".pyc"):
                    continue

                file_path = Path(root) / file
                # Get relative path from skill folder root
                relative_path = file_path.relative_to(skill_folder_path)
                # Add to zip with skill_name as root (so it becomes skill_name/SKILL.md)
                zip_path = f"{skill_name}/{relative_path}"

                zip_file.write(file_path, zip_path)

    zip_buffer.seek(0)
    zip_bytes = zip_buffer.read()

    # Write to output file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(zip_bytes)

    print(f"  ✓ Created {output_path}")


def main() -> None:
    """Zip all default skills and save them to data/skills/."""
    # Get the nexus directory (parent of scripts/)
    nexus_dir = Path(__file__).parent.parent
    skills_source_dir = nexus_dir.parent / "skills" / "skills"
    skills_output_dir = nexus_dir / "data" / "skills"

    skill_folders = [
        "skill-creator",
        "pdf",
        "docx",
        "xlsx",
        "pptx",
        "internal-comms",
    ]

    print(f"Zipping default skills from {skills_source_dir} to {skills_output_dir}...")

    for skill_folder_name in skill_folders:
        skill_folder_path = skills_source_dir / skill_folder_name

        if not skill_folder_path.exists():
            print(f"  ⚠ Skill folder not found: {skill_folder_path}, skipping")
            continue

        try:
            output_path = skills_output_dir / f"{skill_folder_name}.skill"
            zip_skill_folder(skill_folder_path, output_path)
        except Exception as e:
            print(f"  ✗ Failed to zip skill {skill_folder_name}: {e}")
            import traceback

            traceback.print_exc()

    print(f"\n✓ All skills zipped to {skills_output_dir}")


if __name__ == "__main__":
    main()
