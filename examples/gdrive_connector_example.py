"""Google Drive Connector Backend Example.

This example demonstrates how to use the Google Drive connector backend
with OAuth 2.0 authentication for path-based file storage.

Prerequisites:
    1. Install dependencies:
       pip install google-api-python-client google-auth-httplib2

    2. Setup OAuth credentials via CLI:
       nexus oauth setup-gdrive \
           --client-id "123.apps.googleusercontent.com" \
           --client-secret "GOCSPX-..." \
           --user-email "your-email@example.com"

    3. Set encryption key:
       export NEXUS_OAUTH_ENCRYPTION_KEY="your-key-here"
"""

import asyncio
import os
from pathlib import Path

from nexus.backends import GoogleDriveConnectorBackend
from nexus.core.exceptions import AuthenticationError, BackendError, NexusFileNotFoundError
from nexus.core.permissions import OperationContext
from nexus.server.auth import TokenManager


async def main():
    """Main example function."""
    print("=" * 60)
    print("Google Drive Connector Backend Example")
    print("=" * 60)

    # 1. Setup TokenManager
    print("\n[1/6] Initializing TokenManager...")
    home = os.path.expanduser("~")
    db_path = os.path.join(home, ".nexus", "nexus.db")

    token_manager = TokenManager(db_path=db_path)
    print(f"✓ TokenManager initialized (db: {db_path})")

    # 2. Create Google Drive connector backend
    print("\n[2/6] Creating Google Drive connector backend...")
    backend = GoogleDriveConnectorBackend(
        token_manager=token_manager,
        root_folder="nexus-examples",  # Root folder in Drive
        use_shared_drives=False,
        provider="google",
    )
    print(f"✓ Backend created (name: {backend.name})")
    print(f"✓ User-scoped: {backend.user_scoped}")

    # 3. Setup operation context
    print("\n[3/6] Setting up operation context...")
    user_email = os.getenv("NEXUS_USER_EMAIL", "your-email@example.com")

    context = OperationContext(
        user_id=user_email,  # User's email (from OAuth setup)
        tenant_id="example_org",  # Optional tenant
        backend_path="/workspace/example.txt",  # Path in Drive
    )
    print("✓ Context created:")
    print(f"  - User: {context.user_id}")
    print(f"  - Tenant: {context.tenant_id}")
    print(f"  - Path: {context.backend_path}")

    # 4. Write file to Google Drive
    print("\n[4/6] Writing file to Google Drive...")
    try:
        content = b"Hello from Nexus! This file is stored in Google Drive."
        content_hash = backend.write_content(content, context=context)
        print("✓ File written successfully")
        print(f"  - Content hash: {content_hash[:16]}...")
        print(f"  - Location: Google Drive/{backend.root_folder}{context.backend_path}")
    except AuthenticationError as e:
        print(f"✗ Authentication failed: {e}")
        print(
            "  Run: nexus oauth setup-gdrive --client-id ... --client-secret ... --user-email ..."
        )
        return
    except BackendError as e:
        print(f"✗ Backend error: {e}")
        return

    # 5. Read file from Google Drive
    print("\n[5/6] Reading file from Google Drive...")
    try:
        read_content = backend.read_content(content_hash, context=context)
        print("✓ File read successfully")
        print(f"  - Content: {read_content.decode()[:50]}...")
    except NexusFileNotFoundError:
        print("✗ File not found in Drive")
    except BackendError as e:
        print(f"✗ Backend error: {e}")

    # 6. Check file existence
    print("\n[6/6] Checking file existence...")
    exists = backend.content_exists(content_hash, context=context)
    print(f"✓ File exists: {exists}")

    # Cleanup
    print("\n" + "=" * 60)
    print("Example completed!")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Check Google Drive web UI for 'nexus-examples' folder")
    print("2. Try exporting Google Docs: context.backend_path = '/report.gdoc.pdf'")
    print("3. Use with NexusFS for full filesystem operations")

    token_manager.close()


async def google_docs_export_example():
    """Example: Export Google Docs to different formats.

    Note: This requires an existing Google Doc in your Drive.
    Create one manually first, then update the path below.
    """
    print("\n" + "=" * 60)
    print("Google Docs Export Example")
    print("=" * 60)

    # Setup
    home = os.path.expanduser("~")
    db_path = os.path.join(home, ".nexus", "nexus.db")
    token_manager = TokenManager(db_path=db_path)

    backend = GoogleDriveConnectorBackend(
        token_manager=token_manager,
        root_folder="nexus-examples",
    )

    user_email = os.getenv("NEXUS_USER_EMAIL", "your-email@example.com")

    # Export Google Doc as PDF
    print("\n[1/3] Exporting Google Doc as PDF...")
    context_pdf = OperationContext(
        user_id=user_email,
        backend_path="/reports/my-report.gdoc.pdf",  # Update this path
    )

    try:
        pdf_content = backend.read_content("ignored", context=context_pdf)
        print(f"✓ PDF exported ({len(pdf_content)} bytes)")

        # Save locally
        output_path = Path("my-report.pdf")
        output_path.write_bytes(pdf_content)
        print(f"✓ Saved to {output_path}")
    except NexusFileNotFoundError:
        print("✗ Google Doc not found. Create one in Drive first.")
    except Exception as e:
        print(f"✗ Export failed: {e}")

    # Export Google Sheet as CSV
    print("\n[2/3] Exporting Google Sheet as CSV...")
    context_csv = OperationContext(
        user_id=user_email,
        backend_path="/data/my-sheet.gsheet.csv",  # Update this path
    )

    try:
        csv_content = backend.read_content("ignored", context=context_csv)
        print(f"✓ CSV exported ({len(csv_content)} bytes)")
        print(f"✓ Preview: {csv_content.decode()[:100]}...")
    except NexusFileNotFoundError:
        print("✗ Google Sheet not found. Create one in Drive first.")
    except Exception as e:
        print(f"✗ Export failed: {e}")

    # Export Google Slides as PPTX
    print("\n[3/3] Exporting Google Slides as PPTX...")
    context_pptx = OperationContext(
        user_id=user_email,
        backend_path="/presentations/my-deck.gslides.pptx",  # Update this path
    )

    try:
        pptx_content = backend.read_content("ignored", context=context_pptx)
        print(f"✓ PPTX exported ({len(pptx_content)} bytes)")

        # Save locally
        output_path = Path("my-deck.pptx")
        output_path.write_bytes(pptx_content)
        print(f"✓ Saved to {output_path}")
    except NexusFileNotFoundError:
        print("✗ Google Slides not found. Create one in Drive first.")
    except Exception as e:
        print(f"✗ Export failed: {e}")

    token_manager.close()

    print("\n" + "=" * 60)
    print("Export example completed!")
    print("=" * 60)


async def shared_drive_example():
    """Example: Using shared drives (Google Workspace).

    Note: Requires a shared drive and appropriate permissions.
    """
    print("\n" + "=" * 60)
    print("Shared Drive Example")
    print("=" * 60)

    # Setup
    home = os.path.expanduser("~")
    db_path = os.path.join(home, ".nexus", "nexus.db")
    token_manager = TokenManager(db_path=db_path)

    # Get shared drive ID from environment
    shared_drive_id = os.getenv("NEXUS_SHARED_DRIVE_ID")
    if not shared_drive_id:
        print("✗ Set NEXUS_SHARED_DRIVE_ID environment variable")
        print("  Find it in Drive web UI: Shared drives > (your drive) > URL")
        return

    backend = GoogleDriveConnectorBackend(
        token_manager=token_manager,
        root_folder="team-workspace",
        use_shared_drives=True,
        shared_drive_id=shared_drive_id,
    )

    user_email = os.getenv("NEXUS_USER_EMAIL", "your-email@example.com")

    # Write to shared drive
    context = OperationContext(
        user_id=user_email,
        tenant_id="team_org",
        backend_path="/projects/project-a/notes.txt",
    )

    try:
        content = b"Team collaboration notes - accessible by all team members"
        backend.write_content(content, context=context)
        print("✓ File written to shared drive")
        print(f"  Location: {backend.root_folder}{context.backend_path}")
    except Exception as e:
        print(f"✗ Shared drive write failed: {e}")

    token_manager.close()

    print("\n" + "=" * 60)
    print("Shared drive example completed!")
    print("=" * 60)


if __name__ == "__main__":
    # Run main example
    asyncio.run(main())

    # Uncomment to run additional examples:
    # asyncio.run(google_docs_export_example())
    # asyncio.run(shared_drive_example())
