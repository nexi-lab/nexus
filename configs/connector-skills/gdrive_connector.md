---
name: google-drive
description: Google Drive connector mounted at {mount_path}. Access Google Drive files, folders, Docs, Sheets, and Slides through the Nexus filesystem.
---

- **Google Docs**: Exported as DOCX by default. Use extension hint for other formats: `.pdf`, `.odt`, `.html`, `.txt`, `.md` (markdown)
- **Google Sheets**: Exported as XLSX by default. Use extension hint: `.pdf`, `.ods`, `.csv`, `.tsv`
- **Google Slides**: Exported as PPTX by default. Use extension hint: `.pdf`, `.odp`, `.txt`
- **Delete**: Moves files to Google Drive trash (not permanent deletion)
- **Shared Drives**: Supported when configured
