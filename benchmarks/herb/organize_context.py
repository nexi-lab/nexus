#!/usr/bin/env python3
"""
HERB Data Transformation Script

Transforms HERB benchmark product JSON files into a directory structure
optimized for grep/glob search by AI agents.

Usage:
    python3 organize_context.py <input.json> <output_dir>
    python3 organize_context.py --all <input_dir> <output_dir>
    python3 organize_context.py --metadata <metadata_dir> <output_dir>

Output structure:
    _metadata/                   # Global reference data
    ├── customers.jsonl          # Customer info (CUST-ID → name, role, company)
    ├── employees.jsonl          # Employee info (eid_xxx → name, role, location)
    ├── org_structure.jsonl      # Flattened org hierarchy
    └── org_structure.md         # Human-readable org chart

    {product}/
    ├── _meta.json           # team[], customers[]
    ├── slack/
    │   └── {channel}.jsonl  # One file per channel
    ├── docs/
    │   ├── _index.jsonl     # Metadata for all docs
    │   └── {doc_id}.md      # Document content
    ├── meetings/
    │   ├── _index.jsonl     # Metadata for all meetings
    │   ├── {id}.md          # Transcripts
    │   └── {id}_chat.txt    # Chat logs
    ├── prs/
    │   ├── _index.jsonl     # All PR metadata
    │   └── {repo}.jsonl     # PRs grouped by repository
    └── urls.jsonl           # Shared links
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse


def sanitize_filename(name: str) -> str:
    """Sanitize a string to be used as a filename."""
    # Replace spaces and special characters with underscores
    name = re.sub(r'[^\w\-.]', '_', name)
    # Remove consecutive underscores
    name = re.sub(r'_+', '_', name)
    # Remove leading/trailing underscores
    name = name.strip('_')
    return name or 'unnamed'


def extract_repo_from_link(link: str) -> str:
    """Extract repository name from GitHub PR link."""
    # Example: https://github.com/mattermost/mattermost-server/pull/2776
    parsed = urlparse(link)
    path_parts = parsed.path.strip('/').split('/')
    if len(path_parts) >= 2:
        return path_parts[1]  # Return repo name (e.g., mattermost-server)
    return 'unknown'


def write_jsonl(filepath: Path, records: list):
    """Write records to a JSONL file (one JSON object per line)."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


def write_json(filepath: Path, data: dict):
    """Write data to a JSON file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_markdown(filepath: Path, content: str):
    """Write content to a markdown file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


def transform_slack(slack_messages: list, output_dir: Path):
    """Transform Slack messages to JSONL files grouped by channel."""
    slack_dir = output_dir / 'slack'

    # Group messages by channel
    channels = defaultdict(list)
    for msg in slack_messages:
        channel_name = msg.get('Channel', {}).get('name', 'unknown')
        message_data = msg.get('Message', {})
        user_data = message_data.get('User', {})

        flat_msg = {
            'id': msg.get('id', ''),
            'user': user_data.get('userId', ''),
            'ts': user_data.get('timestamp', ''),
            'text': user_data.get('text', ''),
        }

        # Include thread replies if present
        thread_replies = msg.get('ThreadReplies', [])
        if thread_replies:
            flat_msg['replies'] = []
            for reply in thread_replies:
                reply_user = reply.get('User', {})
                flat_msg['replies'].append({
                    'id': reply.get('id', reply_user.get('utterranceID', '')),
                    'user': reply_user.get('userId', ''),
                    'ts': reply_user.get('timestamp', ''),
                    'text': reply_user.get('text', ''),
                })

        # Include reactions if present
        reactions = message_data.get('Reactions', [])
        if reactions:
            flat_msg['reactions'] = reactions

        channels[channel_name].append(flat_msg)

    # Sort each channel by timestamp and write to JSONL
    for channel_name, messages in channels.items():
        messages.sort(key=lambda m: m.get('ts', ''))
        filename = sanitize_filename(channel_name) + '.jsonl'
        write_jsonl(slack_dir / filename, messages)

    return len(channels)


def transform_documents(documents: list, output_dir: Path):
    """Transform documents to Markdown files with index."""
    docs_dir = output_dir / 'docs'

    index_records = []
    for doc in documents:
        doc_id = doc.get('id', 'unknown')
        doc_type = doc.get('type', 'Document')
        author = doc.get('author', 'unknown')
        date = doc.get('date', '')
        link = doc.get('document_link', '')
        content = doc.get('content', '')

        # Extract just the date part if it's a datetime string
        date_short = date.split('T')[0] if 'T' in date else date

        # Create index record
        index_record = {
            'id': doc_id,
            'type': doc_type,
            'author': author,
            'date': date_short,
            'link': link,
        }
        index_records.append(index_record)

        # Create markdown content
        md_content = f"""# {doc_type}

**ID:** {doc_id} | **Author:** {author} | **Date:** {date_short}

---

{content}
"""
        filename = sanitize_filename(doc_id) + '.md'
        write_markdown(docs_dir / filename, md_content)

    # Write index
    write_jsonl(docs_dir / '_index.jsonl', index_records)

    return len(documents)


def transform_meetings(transcripts: list, chats: list, output_dir: Path):
    """Transform meeting transcripts and chats to Markdown and text files."""
    meetings_dir = output_dir / 'meetings'

    # Create chat lookup by ID
    chat_lookup = {}
    for chat in chats:
        chat_id = chat.get('id', '')
        # Chat IDs often have "_chat" suffix corresponding to transcript ID
        base_id = chat_id.replace('_chat', '')
        chat_lookup[base_id] = chat.get('text', '')

    index_records = []
    for transcript in transcripts:
        meeting_id = transcript.get('id', 'unknown')
        doc_type = transcript.get('document_type', 'Meeting')
        date = transcript.get('date', '')
        participants = transcript.get('participants', [])
        content = transcript.get('transcript', '')

        # Extract just the date part
        date_short = date.split('T')[0] if 'T' in date else date

        # Create index record
        index_record = {
            'id': meeting_id,
            'type': doc_type,
            'date': date_short,
            'participants': participants,
        }
        index_records.append(index_record)

        # Create markdown content for transcript
        participants_str = ', '.join(participants) if participants else 'N/A'
        md_content = f"""# {doc_type} - Meeting Transcript

**ID:** {meeting_id} | **Date:** {date_short}
**Participants:** {participants_str}

---

{content}
"""
        filename = sanitize_filename(meeting_id) + '.md'
        write_markdown(meetings_dir / filename, md_content)

        # Write chat log if exists
        if meeting_id in chat_lookup:
            chat_filename = sanitize_filename(meeting_id) + '_chat.txt'
            chat_content = chat_lookup[meeting_id]
            with open(meetings_dir / chat_filename, 'w', encoding='utf-8') as f:
                f.write(chat_content)

    # Write index
    write_jsonl(meetings_dir / '_index.jsonl', index_records)

    return len(transcripts)


def transform_prs(prs: list, output_dir: Path):
    """Transform PRs to JSONL files grouped by repository."""
    prs_dir = output_dir / 'prs'

    # Group PRs by repository
    repos = defaultdict(list)
    all_prs = []

    for pr in prs:
        link = pr.get('link', '')
        repo_name = extract_repo_from_link(link)

        # Flatten PR data
        flat_pr = {
            'id': pr.get('id', ''),
            'number': int(pr.get('number', 0)) if pr.get('number') else 0,
            'title': pr.get('title', ''),
            'summary': pr.get('summary', ''),
            'user': pr.get('user', {}).get('login', '') if isinstance(pr.get('user'), dict) else pr.get('user', ''),
            'state': pr.get('state', ''),
            'merged': pr.get('merged', 'False').lower() == 'true' if isinstance(pr.get('merged'), str) else bool(pr.get('merged')),
            'mergeable': pr.get('mergeable', 'False').lower() == 'true' if isinstance(pr.get('mergeable'), str) else bool(pr.get('mergeable')),
            'created_at': pr.get('created_at', ''),
            'link': link,
            'repo': repo_name,
        }

        # Extract reviewers
        reviews = pr.get('reviews', [])
        if reviews:
            flat_pr['reviewers'] = []
            for review in reviews:
                reviewer = review.get('user', {}).get('login', '') if isinstance(review.get('user'), dict) else ''
                if reviewer and reviewer not in flat_pr['reviewers']:
                    flat_pr['reviewers'].append(reviewer)
                flat_pr.setdefault('review_states', []).append({
                    'user': reviewer,
                    'state': review.get('state', ''),
                    'comment': review.get('comment', ''),
                })

        repos[repo_name].append(flat_pr)
        all_prs.append(flat_pr)

    # Write index with all PRs
    write_jsonl(prs_dir / '_index.jsonl', all_prs)

    # Write PRs grouped by repository
    for repo_name, pr_list in repos.items():
        filename = sanitize_filename(repo_name) + '.jsonl'
        write_jsonl(prs_dir / filename, pr_list)

    return len(prs), len(repos)


def transform_urls(urls: list, output_dir: Path):
    """Transform URLs to JSONL file."""
    if not urls:
        return 0

    records = []
    for url in urls:
        records.append({
            'id': url.get('id', ''),
            'link': url.get('link', ''),
            'description': url.get('description', ''),
        })

    write_jsonl(output_dir / 'urls.jsonl', records)
    return len(urls)


def flatten_org_member(member: dict, vp_id: str = None, lead_id: str = None, role_type: str = None) -> dict:
    """Flatten an org member with hierarchy info."""
    return {
        'employee_id': member.get('employee_id', ''),
        'name': member.get('name', ''),
        'role': member.get('role', ''),
        'location': member.get('location', ''),
        'org': member.get('org', ''),
        'role_type': role_type,
        'reports_to_vp': vp_id,
        'reports_to_lead': lead_id,
    }


def transform_metadata(metadata_dir: Path, output_dir: Path):
    """Transform HERB metadata files to grep-friendly format."""
    meta_out = output_dir / '_metadata'
    meta_out.mkdir(parents=True, exist_ok=True)

    stats = {}

    # Transform customers_data.json
    customers_file = metadata_dir / 'customers_data.json'
    if customers_file.exists():
        with open(customers_file, encoding='utf-8') as f:
            customers = json.load(f)

        # Already a list, just write as JSONL
        records = []
        for c in customers:
            records.append({
                'id': c.get('id', ''),
                'name': c.get('name', ''),
                'role': c.get('role', ''),
                'company': c.get('company', ''),
            })
        write_jsonl(meta_out / 'customers.jsonl', records)
        stats['customers'] = len(records)
        print(f"  ✓ Transformed {len(records)} customers")

    # Transform employee.json
    employees_file = metadata_dir / 'employee.json'
    if employees_file.exists():
        with open(employees_file, encoding='utf-8') as f:
            employees = json.load(f)

        # Convert dict to list of records
        records = []
        for eid, emp in employees.items():
            records.append({
                'id': eid,
                'name': emp.get('name', ''),
                'role': emp.get('role', ''),
                'location': emp.get('location', ''),
                'org': emp.get('org', ''),
            })
        # Sort by employee ID for consistent output
        records.sort(key=lambda x: x['id'])
        write_jsonl(meta_out / 'employees.jsonl', records)
        stats['employees'] = len(records)
        print(f"  ✓ Transformed {len(records)} employees")

    # Transform salesforce_team.json (org structure)
    team_file = metadata_dir / 'salesforce_team.json'
    if team_file.exists():
        with open(team_file, encoding='utf-8') as f:
            teams = json.load(f)

        # Flatten the hierarchical structure
        flat_records = []
        md_lines = ["# Salesforce Organization Structure\n"]

        for vp in teams:
            vp_id = vp.get('employee_id', '')
            vp_name = vp.get('name', '')
            vp_role = vp.get('role', '')
            vp_org = vp.get('org', '')

            # Add VP record
            flat_records.append(flatten_org_member(vp, role_type='vp'))

            md_lines.append(f"\n## {vp_name} ({vp_role})")
            md_lines.append(f"**ID:** {vp_id} | **Org:** {vp_org}\n")

            # Process engineering leads
            for lead in vp.get('engineering_leads', []):
                lead_id = lead.get('employee_id', '')
                lead_name = lead.get('name', '')

                flat_records.append(flatten_org_member(lead, vp_id=vp_id, role_type='engineering_lead'))

                md_lines.append(f"\n### {lead_name} (Engineering Lead)")
                md_lines.append(f"**ID:** {lead_id}")

                # Engineers under this lead
                engineers = lead.get('engineers', [])
                if engineers:
                    md_lines.append("\n**Engineers:**")
                    for eng in engineers:
                        flat_records.append(flatten_org_member(eng, vp_id=vp_id, lead_id=lead_id, role_type='engineer'))
                        md_lines.append(f"- {eng.get('name')} ({eng.get('employee_id')}) - {eng.get('location')}")

                # QA specialists under this lead
                qa_list = lead.get('qa_specialists', [])
                if qa_list:
                    md_lines.append("\n**QA Specialists:**")
                    for qa in qa_list:
                        flat_records.append(flatten_org_member(qa, vp_id=vp_id, lead_id=lead_id, role_type='qa_specialist'))
                        md_lines.append(f"- {qa.get('name')} ({qa.get('employee_id')}) - {qa.get('location')}")

            # Process other team types directly under VP
            for team_type in ['product_managers', 'tech_architects', 'ux_researchers',
                              'marketing_research_analysts', 'chief_product_officers', 'marketing_managers']:
                members = vp.get(team_type, [])
                if members:
                    role_label = team_type.replace('_', ' ').title()
                    md_lines.append(f"\n### {role_label}")
                    for member in members:
                        flat_records.append(flatten_org_member(member, vp_id=vp_id, role_type=team_type.rstrip('s')))
                        md_lines.append(f"- {member.get('name')} ({member.get('employee_id')}) - {member.get('location')}")

        write_jsonl(meta_out / 'org_structure.jsonl', flat_records)
        write_markdown(meta_out / 'org_structure.md', '\n'.join(md_lines))
        stats['org_members'] = len(flat_records)
        print(f"  ✓ Transformed {len(flat_records)} org structure members")

    return stats


def transform_product(input_file: Path, output_dir: Path):
    """Transform a single product JSON file."""
    print(f"Processing {input_file.name}...")

    with open(input_file, encoding='utf-8') as f:
        data = json.load(f)

    # Get product name from filename
    product_name = input_file.stem
    product_dir = output_dir / product_name
    product_dir.mkdir(parents=True, exist_ok=True)

    # Write meta file
    meta = {
        'product': product_name,
        'team': data.get('team', []),
        'customers': data.get('customers', []),
    }
    write_json(product_dir / '_meta.json', meta)

    # Transform each section
    stats = {
        'product': product_name,
        'team_count': len(meta['team']),
        'customer_count': len(meta['customers']),
    }

    # Slack
    if 'slack' in data:
        channel_count = transform_slack(data['slack'], product_dir)
        stats['slack_channels'] = channel_count
        stats['slack_messages'] = len(data['slack'])

    # Documents
    if 'documents' in data:
        doc_count = transform_documents(data['documents'], product_dir)
        stats['documents'] = doc_count

    # Meetings
    if 'meeting_transcripts' in data or 'meeting_chats' in data:
        meeting_count = transform_meetings(
            data.get('meeting_transcripts', []),
            data.get('meeting_chats', []),
            product_dir
        )
        stats['meetings'] = meeting_count

    # PRs
    if 'prs' in data:
        pr_count, repo_count = transform_prs(data['prs'], product_dir)
        stats['prs'] = pr_count
        stats['pr_repos'] = repo_count

    # URLs
    if 'urls' in data:
        url_count = transform_urls(data['urls'], product_dir)
        stats['urls'] = url_count

    print(f"  ✓ Created {product_dir}")
    return stats


def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python3 organize_context.py <input.json> <output_dir>")
        print("  python3 organize_context.py --all <input_dir> <output_dir>")
        print("  python3 organize_context.py --metadata <metadata_dir> <output_dir>")
        sys.exit(1)

    if sys.argv[1] == '--metadata':
        # Process metadata files
        metadata_dir = Path(sys.argv[2])
        output_dir = Path(sys.argv[3])

        if not metadata_dir.exists():
            print(f"Error: {metadata_dir} not found")
            sys.exit(1)

        print(f"Processing metadata from {metadata_dir}...")
        stats = transform_metadata(metadata_dir, output_dir)
        print("\n✓ Metadata transformation complete")
        print(f"Stats: {json.dumps(stats, indent=2)}")

    elif sys.argv[1] == '--all':
        # Process all JSON files in directory
        input_dir = Path(sys.argv[2])
        output_dir = Path(sys.argv[3])

        json_files = list(input_dir.glob('*.json'))
        if not json_files:
            print(f"No JSON files found in {input_dir}")
            sys.exit(1)

        print(f"Found {len(json_files)} product files to process")
        all_stats = []

        for json_file in sorted(json_files):
            # Skip non-product files
            if json_file.name.startswith('_'):
                continue
            stats = transform_product(json_file, output_dir)
            all_stats.append(stats)

        # Write summary
        summary_file = output_dir / '_summary.json'
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(all_stats, f, indent=2)

        print(f"\n✓ Processed {len(all_stats)} products")
        print(f"✓ Summary written to {summary_file}")

    else:
        # Process single file
        input_file = Path(sys.argv[1])
        output_dir = Path(sys.argv[2])

        if not input_file.exists():
            print(f"Error: {input_file} not found")
            sys.exit(1)

        stats = transform_product(input_file, output_dir)
        print(f"\nStats: {json.dumps(stats, indent=2)}")


if __name__ == '__main__':
    main()
