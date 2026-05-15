"""Gmail connector utility functions for email categorization and listing."""

import logging
import time
from collections.abc import Callable
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class FolderStats(TypedDict):
    """Type for folder statistics."""

    emails: int
    threads: set[str]


def get_email_folder(email_labels: list[str]) -> str | None:
    """Determine which folder an email belongs to based on priority.

    Priority order:
    1. SENT - All sent emails
    2. STARRED - Starred emails in INBOX (excluding SENT)
    3. IMPORTANT - Important emails in INBOX (excluding SENT, STARRED)
    4. INBOX - Remaining INBOX emails

    Args:
        email_labels: List of Gmail label IDs for the email

    Returns:
        Folder name (SENT, STARRED, IMPORTANT, or INBOX), or None if doesn't match any
    """
    # Priority 1: SENT
    if "SENT" in email_labels:
        return "SENT"

    # Check if email is in INBOX (required for remaining priorities)
    if "INBOX" not in email_labels:
        return None

    # Priority 2: STARRED in INBOX
    if "STARRED" in email_labels:
        return "STARRED"

    # Priority 3: IMPORTANT in INBOX
    if "IMPORTANT" in email_labels:
        return "IMPORTANT"

    # Priority 4: Remaining INBOX emails
    return "INBOX"


def list_emails_by_folder(
    service: Any,
    user_id: str = "me",
    max_results: int | None = 2000,
    folder_filter: list[str] | None = None,
    silent: bool = False,
) -> list[dict[str, Any]]:
    """List emails from Gmail with paths organized by label/thread structure.

    Uses labelIds filtering in messages.list() to avoid individual get() calls.

    Args:
        service: Gmail API service object
        user_id: Gmail user ID (default: "me")
        max_results: Maximum number of emails to fetch PER LABEL (None for all, default: 2000)
        folder_filter: Optional list of folders to fetch (e.g., ["INBOX", "SENT"]). If None, fetches all folders.
        silent: If True, suppress progress output (default: False)

    Returns:
        List of email metadata objects with path field:
        [
            {
                "id": "msg_id",
                "threadId": "thread_id",
                "labelIds": ["SENT"],
                "path": "SENT/thread_id/email-msg_id.yaml",
                "folder": "SENT"
            },
            ...
        ]
    """
    # Initialize result list
    all_emails = []

    # Default to all folders if no filter provided
    if folder_filter is None:
        folder_filter = ["SENT", "STARRED", "IMPORTANT", "INBOX"]

    # Track folder statistics for summary
    folder_stats: dict[str, FolderStats] = {
        "SENT": {"emails": 0, "threads": set()},
        "STARRED": {"emails": 0, "threads": set()},
        "IMPORTANT": {"emails": 0, "threads": set()},
        "INBOX": {"emails": 0, "threads": set()},
    }

    # Track message IDs we've already categorized (to respect priority)
    seen_ids = set()

    # Define folder priority order
    folder_priority = ["SENT", "STARRED", "IMPORTANT", "INBOX"]

    # Determine which folders need to be fetched to respect priority
    # If requesting a lower priority folder, we must also fetch higher priority folders
    # to properly exclude those messages
    folders_to_fetch = []
    for folder in folder_priority:
        folders_to_fetch.append(folder)
        if folder in folder_filter and (
            folder == folder_filter[-1] or all(f in folders_to_fetch for f in folder_filter)
        ):
            # Found the lowest priority folder requested, we have all we need
            break

    def fetch_with_labels(label_ids: list[str], limit: int | None = None) -> list[dict[str, Any]]:
        """Fetch messages with specific labels."""
        messages = []
        page_token = None

        while True:
            request_params = {
                "userId": user_id,
                "labelIds": label_ids,
                "maxResults": 500,  # Max per page
            }
            if page_token:
                request_params["pageToken"] = page_token

            # Exponential backoff for rate limiting
            max_retries = 5
            base_delay = 1.0
            result = None

            for retry in range(max_retries):
                try:
                    result = service.users().messages().list(**request_params).execute()
                    break  # Success
                except Exception as e:
                    error_str = str(e)
                    if "429" in error_str or "rateLimitExceeded" in error_str:
                        if retry < max_retries - 1:
                            delay = base_delay * (2**retry)
                            logger.warning(
                                f"[LIST-EMAILS] Rate limit hit (429), retrying in {delay}s "
                                f"(attempt {retry + 1}/{max_retries})"
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                f"[LIST-EMAILS] Rate limit exceeded after {max_retries} retries"
                            )
                            raise
                    else:
                        # Non-rate-limit error
                        logger.error(f"[LIST-EMAILS] Failed to list messages: {e}")
                        raise

            if result is None:
                break

            page_messages = result.get("messages", [])

            for msg in page_messages:
                if msg["id"] not in seen_ids:
                    messages.append(msg)
                    if limit and len(messages) >= limit:
                        return messages

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return messages

    # Priority 1: SENT emails
    if "SENT" in folders_to_fetch:
        if not silent and "SENT" in folder_filter:
            print("📥 Fetching SENT emails...")
        sent_messages = fetch_with_labels(["SENT"], max_results)
        for msg in sent_messages:
            thread_id = msg["threadId"]
            message_id = msg["id"]
            if "SENT" in folder_filter:
                all_emails.append(
                    {
                        "id": message_id,
                        "threadId": thread_id,
                        "labelIds": ["SENT"],
                        "path": f"SENT/{thread_id}/email-{message_id}.yaml",
                        "folder": "SENT",
                    }
                )
                folder_stats["SENT"]["emails"] += 1
                folder_stats["SENT"]["threads"].add(thread_id)
            seen_ids.add(message_id)
        if not silent and "SENT" in folder_filter:
            print(
                f"   Found {len(sent_messages)} SENT emails in {len(folder_stats['SENT']['threads'])} threads"
            )

    # Priority 2: STARRED in INBOX (excluding SENT)
    if "STARRED" in folders_to_fetch:
        if not silent and "STARRED" in folder_filter:
            print("📥 Fetching STARRED + INBOX emails...")
        starred_messages = fetch_with_labels(["STARRED", "INBOX"], max_results)
        for msg in starred_messages:
            message_id = msg["id"]
            if message_id not in seen_ids:
                thread_id = msg["threadId"]
                if "STARRED" in folder_filter:
                    all_emails.append(
                        {
                            "id": message_id,
                            "threadId": thread_id,
                            "labelIds": ["STARRED", "INBOX"],
                            "path": f"STARRED/{thread_id}/email-{message_id}.yaml",
                            "folder": "STARRED",
                        }
                    )
                    folder_stats["STARRED"]["emails"] += 1
                    folder_stats["STARRED"]["threads"].add(thread_id)
                seen_ids.add(message_id)
        if not silent and "STARRED" in folder_filter:
            print(
                f"   Found {folder_stats['STARRED']['emails']} STARRED emails in {len(folder_stats['STARRED']['threads'])} threads (excluding SENT)"
            )

    # Priority 3: IMPORTANT in INBOX (excluding SENT, STARRED)
    if "IMPORTANT" in folders_to_fetch:
        if not silent and "IMPORTANT" in folder_filter:
            print("📥 Fetching IMPORTANT + INBOX emails...")
        important_messages = fetch_with_labels(["IMPORTANT", "INBOX"], max_results)
        for msg in important_messages:
            message_id = msg["id"]
            if message_id not in seen_ids:
                thread_id = msg["threadId"]
                if "IMPORTANT" in folder_filter:
                    all_emails.append(
                        {
                            "id": message_id,
                            "threadId": thread_id,
                            "labelIds": ["IMPORTANT", "INBOX"],
                            "path": f"IMPORTANT/{thread_id}/email-{message_id}.yaml",
                            "folder": "IMPORTANT",
                        }
                    )
                    folder_stats["IMPORTANT"]["emails"] += 1
                    folder_stats["IMPORTANT"]["threads"].add(thread_id)
                seen_ids.add(message_id)
        if not silent and "IMPORTANT" in folder_filter:
            print(
                f"   Found {folder_stats['IMPORTANT']['emails']} IMPORTANT emails in {len(folder_stats['IMPORTANT']['threads'])} threads (excluding SENT, STARRED)"
            )

    # Priority 4: Remaining INBOX emails
    if "INBOX" in folders_to_fetch:
        if not silent and "INBOX" in folder_filter:
            print("📥 Fetching remaining INBOX emails...")
        inbox_messages = fetch_with_labels(["INBOX"], max_results)
        for msg in inbox_messages:
            message_id = msg["id"]
            if message_id not in seen_ids:
                thread_id = msg["threadId"]
                if "INBOX" in folder_filter:
                    all_emails.append(
                        {
                            "id": message_id,
                            "threadId": thread_id,
                            "labelIds": ["INBOX"],
                            "path": f"INBOX/{thread_id}/email-{message_id}.yaml",
                            "folder": "INBOX",
                        }
                    )
                    folder_stats["INBOX"]["emails"] += 1
                    folder_stats["INBOX"]["threads"].add(thread_id)
                seen_ids.add(message_id)
        if not silent and "INBOX" in folder_filter:
            print(
                f"   Found {folder_stats['INBOX']['emails']} remaining INBOX emails in {len(folder_stats['INBOX']['threads'])} threads"
            )

    if not silent:
        print(f"\n✅ Categorized {len(seen_ids)} total emails")

        # Print summary
        print("\n📋 Summary:")
        for folder in ["SENT", "STARRED", "IMPORTANT", "INBOX"]:
            stats = folder_stats[folder]
            print(f"   {folder}: {stats['emails']} emails in {len(stats['threads'])} threads")

    return all_emails


def print_folder_statistics(emails: list[dict[str, Any]]) -> None:
    """Print detailed statistics about email folders grouped by threads.

    Args:
        emails: List of email objects with folder and thread information
    """
    print("\n" + "=" * 80)
    print("FOLDER STATISTICS (GROUPED BY THREADS)")
    print("=" * 80)

    # Group by folder
    folder_groups: dict[str, list[dict]] = {
        "SENT": [],
        "STARRED": [],
        "IMPORTANT": [],
        "INBOX": [],
    }

    for email in emails:
        folder = email.get("folder")
        if folder in folder_groups:
            folder_groups[folder].append(email)

    total_emails = len(emails)

    # Count unique threads across all folders
    all_threads = set()
    for folder_emails in folder_groups.values():
        for email in folder_emails:
            all_threads.add(email["threadId"])

    print(f"\n📊 Total emails: {total_emails}")
    print(f"\n📊 Total threads: {len(all_threads)}")

    for folder in ["SENT", "STARRED", "IMPORTANT", "INBOX"]:
        folder_emails = folder_groups[folder]
        num_emails = len(folder_emails)

        # Count unique threads in this folder
        folder_threads = {email["threadId"] for email in folder_emails}
        num_threads = len(folder_threads)

        percentage = (num_emails / total_emails * 100) if total_emails > 0 else 0

        print(f"\n📁 {folder}:")
        print(f"   Emails: {num_emails} ({percentage:.1f}%)")
        print(f"   Threads: {num_threads}")

        # Show first 3 emails as samples
        if folder_emails:
            print("   Sample paths:")
            for email in folder_emails[:5]:
                print(f"      {email['path']}")
            if num_emails > 5:
                print(f"      ... and {num_emails - 5} more")


def fetch_emails_batch(
    service: Any,
    message_ids: list[str],
    parse_message_func: Any,
    email_cache: dict[str, Any],
) -> None:
    """Fetch multiple emails in a single batch request using Gmail API.

    Args:
        service: Gmail API service object
        message_ids: List of message IDs to fetch
        parse_message_func: Function to parse Gmail message response (e.g., _parse_gmail_message)
        email_cache: Cache dict to populate with fetched emails (modified in-place)
    """
    # Gmail batch requests - reduce to 50 to avoid rate limiting
    batch_size = 50
    for i in range(0, len(message_ids), batch_size):
        batch_ids = message_ids[i : i + batch_size]

        # Track failed message IDs for retry
        failed_429_ids: list[str] = []

        def make_callback(
            failed_list: list[str],
        ) -> Callable[[str, Any, Exception | None], None]:
            """Factory to create callback with proper closure."""

            def _callback(request_id: str, response: Any, exception: Exception | None) -> None:
                """Callback for batch request."""
                if exception:
                    error_str = str(exception)
                    # Check if this individual message failed with 429
                    if "429" in error_str or "rateLimitExceeded" in error_str:
                        failed_list.append(request_id)
                        # Don't log 429 errors here - they'll be retried and logged only if retries fail
                    else:
                        # Log non-429 errors immediately (these won't be retried)
                        logger.warning(
                            f"[BATCH-FETCH] Error fetching message {request_id}: {exception}"
                        )
                    return  # Skip on error

                if response and request_id:
                    try:
                        message_id = request_id
                        email_data = parse_message_func(response)
                        email_cache[message_id] = email_data
                    except Exception as e:
                        logger.warning(f"[BATCH-FETCH] Error parsing message {request_id}: {e}")
                        pass  # Skip on parse error

            return _callback

        callback = make_callback(failed_429_ids)

        # Exponential backoff for rate limiting (429 errors)
        max_retries = 5
        base_delay = 1.0  # Start with 1 second
        ids_to_fetch = batch_ids  # Start with all message IDs

        for retry in range(max_retries):
            # Reset failed IDs for this retry attempt
            failed_429_ids.clear()

            # Use service.new_batch_http_request() instead of deprecated BatchHttpRequest()
            batch = service.new_batch_http_request()

            if not ids_to_fetch:
                break  # No messages to fetch

            for message_id in ids_to_fetch:
                request = service.users().messages().get(userId="me", id=message_id, format="full")
                batch.add(request, callback=callback, request_id=message_id)

            try:
                batch.execute()

                # Check if any individual messages failed with 429
                if failed_429_ids:
                    if retry < max_retries - 1:
                        # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                        delay = base_delay * (2**retry)
                        logger.warning(
                            f"[BATCH-FETCH] {len(failed_429_ids)} messages hit rate limit (429), "
                            f"retrying in {delay}s (attempt {retry + 1}/{max_retries})"
                        )
                        time.sleep(delay)
                        # Set up next iteration to retry only the failed messages
                        ids_to_fetch = failed_429_ids.copy()
                        continue  # Retry with failed messages
                    else:
                        logger.warning(
                            f"[BATCH-FETCH] {len(failed_429_ids)} messages still failing after "
                            f"{max_retries} retries: {failed_429_ids[:5]}..."
                        )

                # Success - no failures or retries exhausted
                break

            except Exception as e:
                error_str = str(e)

                # Check if the entire batch request failed with 429
                if "429" in error_str or "rateLimitExceeded" in error_str:
                    if retry < max_retries - 1:
                        # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                        delay = base_delay * (2**retry)
                        logger.warning(
                            f"[BATCH-FETCH] Batch request hit rate limit (429), retrying in {delay}s "
                            f"(attempt {retry + 1}/{max_retries}) for {len(ids_to_fetch)} messages"
                        )
                        time.sleep(delay)
                        # Keep same ids_to_fetch for next iteration
                    else:
                        logger.warning(
                            f"[BATCH-FETCH] Batch rate limit exceeded after {max_retries} retries "
                            f"for {len(ids_to_fetch)} messages"
                        )
                else:
                    # Non-rate-limit error - log and break
                    logger.warning(
                        f"[BATCH-FETCH] Batch execute failed for {len(ids_to_fetch)} messages: {e}"
                    )
                    break
