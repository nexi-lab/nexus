# Building the Documentation

This guide covers how to build and deploy the Nexus documentation site.

## Prerequisites

```bash
pip install mkdocs-material mkdocs-git-revision-date-localized-plugin mkdocs-minify-plugin
```

## Local Development

### Serve Locally

```bash
# Start development server with live reload
mkdocs serve

# Open http://127.0.0.1:8000 in your browser
```

The site will automatically reload when you make changes to any documentation files.

### Build Site

```bash
# Build static site
mkdocs build

# Output will be in ./site/
```

## Deployment

### GitHub Pages

```bash
# Deploy to GitHub Pages (gh-pages branch)
mkdocs gh-deploy --force

# With custom message
mkdocs gh-deploy --message "Update documentation" --force
```

### Manual Deployment

```bash
# Build the site
mkdocs build

# Upload the ./site/ directory to your hosting provider
rsync -avz site/ user@server:/var/www/docs/
```

## Documentation Structure

```
docs/
├── index.md                    # Landing page with hero section
├── getting-started/
│   ├── index.md               # Getting started overview
│   ├── installation.md
│   ├── quickstart.md
│   ├── configuration.md
│   └── deployment-modes.md
├── api/
│   ├── index.md               # API reference overview
│   ├── core-api.md
│   ├── file-operations.md
│   └── ...
├── deployment/
│   ├── index.md               # Deployment guide overview
│   ├── server-setup.md
│   └── postgresql.md
├── development/
│   ├── index.md               # Development guide overview
│   └── development.md
├── stylesheets/
│   └── extra.css              # Custom CSS (beautiful!)
└── javascripts/
    └── extra.js               # Interactive features
```

## Custom Styling

The documentation includes extensive custom styling:

### CSS Features
- **Hero section** with gradient background and animations
- **Feature cards** with hover effects
- **Stats counters** with animated numbers
- **Comparison tables** with gradient headers
- **Code blocks** with copy buttons
- **Responsive design** for all screen sizes
- **Dark mode** support

### JavaScript Features
- **Animated counters** for statistics
- **Smooth scrolling** for anchor links
- **Copy code buttons** on hover
- **Search enhancements** with keyboard shortcuts (Ctrl/Cmd+K)
- **Scroll progress indicator**
- **External link icons**

## Writing Documentation

### Use Custom Components

The documentation supports custom HTML/CSS classes:

#### Feature Cards

```markdown
<div class="features-grid" markdown>

<div class="feature-card" markdown>
### :material-icon: Title
Description text here.

[Button Text →](link.md){ .md-button }
</div>

</div>
```

#### Stats

```markdown
<div class="stats-grid" markdown>

<div class="stat-card" markdown>
### 10K+
**Downloads**
</div>

</div>
```

#### Comparison Table

```markdown
<div class="comparison-table" markdown>

| Feature | Option A | Option B | **Nexus** |
|---------|----------|----------|-----------|
| Feature 1 | ❌ | ✅ | ✅ |

</div>
```

### Icons

Use Material Icons in headings:

```markdown
### :material-rocket: Getting Started
### :octicons-check-24: Features
### :fontawesome-brands-github: GitHub
```

Browse all icons at https://squidfunk.github.io/mkdocs-material/reference/icons-emojis/

### Admonitions

```markdown
!!! note "Note Title"
    This is a note.

!!! tip "Pro Tip"
    This is helpful information.

!!! warning "Warning"
    Be careful with this.

!!! danger "Danger"
    This is critical.
```

### Tabs

```markdown
=== "Python"
    ```python
    import nexus
    ```

=== "CLI"
    ```bash
    nexus --help
    ```
```

## Quality Checks

### Before Committing

```bash
# Check for broken links
mkdocs build --strict

# Spell check (optional)
# pip install pyspelling
# pyspelling -c .spellcheck.yml
```

### Preview Changes

Always preview your changes locally before committing:

```bash
mkdocs serve
# Visit http://127.0.0.1:8000
```

## Tips for Great Documentation

1. **Use examples** - Show, don't just tell
2. **Add code snippets** - Make them copy-pasteable
3. **Include diagrams** - Use Mermaid for flow charts
4. **Write clearly** - Short sentences, simple words
5. **Test your code** - Ensure all examples work
6. **Update regularly** - Keep docs in sync with code
7. **Use admonitions** - Highlight important information
8. **Add navigation** - Use clear section headers
9. **Include search keywords** - Think about how users search
10. **Get feedback** - Ask users if docs are helpful

## Advanced Features

### Mermaid Diagrams

```markdown
\`\`\`mermaid
graph LR
    A[Client] --> B[Server]
    B --> C[Database]
\`\`\`
```

### Math

```markdown
$$
E = mc^2
$$
```

### Footnotes

```markdown
Here's a sentence with a footnote[^1].

[^1]: This is the footnote.
```

## Troubleshooting

### Build Fails

```bash
# Clear cache
rm -rf site/
mkdocs build

# Update dependencies
pip install --upgrade mkdocs-material
```

### Styling Issues

Check the browser console for CSS/JS errors:
- Open DevTools (F12)
- Look for errors in Console tab
- Check Network tab for failed resource loads

### Slow Build

```bash
# Disable git plugin for faster local builds
# Comment out in mkdocs.yml:
# - git-revision-date-localized-plugin

mkdocs build
```

## Resources

- [MkDocs Documentation](https://www.mkdocs.org/)
- [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)
- [Material Icons](https://squidfunk.github.io/mkdocs-material/reference/icons-emojis/)
- [Markdown Guide](https://www.markdownguide.org/)
