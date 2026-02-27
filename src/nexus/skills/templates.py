"""Skill templates for creating new skills.

Provides pre-built templates for common skill patterns:
- basic: Simple skill template
- data-analysis: Data processing and analysis
- code-generation: Code generation and modification
- document-processing: Document parsing and extraction
- api-integration: External API integration
"""

from typing import Any

TEMPLATES = {
    "basic": """# {name}

{description}

## Overview

This skill provides basic functionality.

## Usage

1. Define the task
2. Process the inputs
3. Return the results

## Example

```
Input: Example input
Output: Example output
```

## Notes

- Add any important notes here
- Document limitations or requirements
""",
    "data-analysis": """# {name}

{description}

## Overview

This skill analyzes data and extracts insights.

## Capabilities

- **Data Loading**: Load data from various sources
- **Data Cleaning**: Handle missing values, outliers, duplicates
- **Statistical Analysis**: Descriptive statistics, distributions
- **Visualization**: Generate charts and plots
- **Reporting**: Summarize findings

## Workflow

1. **Load Data**
   - Read from files (CSV, JSON, Excel)
   - Connect to databases
   - Validate data schema

2. **Clean & Transform**
   - Handle missing values
   - Remove duplicates
   - Normalize/standardize values
   - Feature engineering

3. **Analyze**
   - Descriptive statistics (mean, median, std)
   - Correlation analysis
   - Trend detection
   - Outlier identification

4. **Visualize**
   - Distribution plots
   - Time series charts
   - Correlation heatmaps
   - Custom visualizations

5. **Report**
   - Key findings summary
   - Statistical insights
   - Recommendations
   - Export results

## Example

```python
# Load data
data = load_csv("sales_data.csv")

# Clean data
data = remove_duplicates(data)
data = fill_missing_values(data, strategy="mean")

# Analyze
stats = calculate_statistics(data, columns=["revenue", "units"])
trends = detect_trends(data, date_column="date", value_column="revenue")

# Report
generate_report(stats, trends, output="analysis_report.pdf")
```

## Dependencies

- Data manipulation: pandas
- Visualization: matplotlib, seaborn
- Statistics: numpy, scipy

## Notes

- Ensure data quality before analysis
- Document assumptions and limitations
- Validate results with domain experts
""",
    "code-generation": """# {name}

{description}

## Overview

This skill generates and modifies code based on requirements.

## Capabilities

- **Code Generation**: Create new code from specifications
- **Code Modification**: Update existing code
- **Refactoring**: Improve code structure and quality
- **Documentation**: Generate docstrings and comments
- **Testing**: Create unit tests

## Workflow

1. **Analyze Requirements**
   - Parse user specifications
   - Identify language and framework
   - Extract constraints and requirements

2. **Design Solution**
   - Choose appropriate patterns
   - Plan code structure
   - Define interfaces

3. **Generate Code**
   - Write implementation
   - Add error handling
   - Include logging
   - Follow style guidelines

4. **Add Documentation**
   - Write docstrings
   - Add inline comments
   - Create usage examples

5. **Generate Tests**
   - Create unit tests
   - Add edge cases
   - Ensure coverage

6. **Review & Refine**
   - Check for bugs
   - Optimize performance
   - Ensure readability

## Example

```python
# Generate a function
generate_function(
    name="calculate_average",
    parameters=[("numbers", "list[float]")],
    return_type="float",
    docstring="Calculate the average of a list of numbers",
    implementation="return sum(numbers) / len(numbers) if numbers else 0"
)

# Refactor existing code
refactor_code(
    source_file="legacy_module.py",
    operations=[
        "extract_method",
        "rename_variables",
        "add_type_hints"
    ]
)
```

## Best Practices

- Follow language-specific conventions (PEP 8 for Python, etc.)
- Add comprehensive error handling
- Write clear, self-documenting code
- Include type hints where applicable
- Generate tests alongside code

## Supported Languages

- Python
- JavaScript/TypeScript
- Java
- Go
- Rust

## Notes

- Always validate generated code
- Run tests before deploying
- Consider security implications
""",
    "document-processing": """# {name}

{description}

## Overview

This skill processes and extracts information from documents.

## Capabilities

- **Document Parsing**: PDF, Word, Excel, CSV, JSON
- **Text Extraction**: Extract text from various formats
- **Structure Analysis**: Identify sections, tables, lists
- **Entity Recognition**: Extract names, dates, numbers
- **Summarization**: Generate document summaries
- **Search**: Find specific information

## Workflow

1. **Load Document**
   - Detect file type
   - Choose appropriate parser
   - Read document content

2. **Extract Structure**
   - Identify sections and headings
   - Parse tables and lists
   - Extract metadata

3. **Extract Content**
   - Plain text extraction
   - Preserve formatting when needed
   - Handle special characters

4. **Process & Analyze**
   - Entity recognition (NER)
   - Keyword extraction
   - Sentiment analysis
   - Topic modeling

5. **Transform**
   - Convert to different formats
   - Generate summaries
   - Create structured data

6. **Output**
   - Export to JSON/CSV/Markdown
   - Generate reports
   - Store in database

## Example

```python
# Process a PDF document
doc = load_document("report.pdf")

# Extract text
text = extract_text(doc)

# Parse structure
sections = extract_sections(doc)
tables = extract_tables(doc)

# Extract entities
entities = extract_entities(text)
dates = entities.filter(type="DATE")
amounts = entities.filter(type="MONEY")

# Generate summary
summary = summarize(text, max_length=200)

# Export
export_json({{
    "summary": summary,
    "sections": sections,
    "tables": tables,
    "entities": entities
}}, output="document_analysis.json")
```

## Supported Formats

- **Text**: TXT, MD, RTF
- **Documents**: PDF, DOCX, DOC
- **Spreadsheets**: XLSX, XLS, CSV
- **Structured**: JSON, XML, YAML
- **Images**: PNG, JPG (with OCR)

## Dependencies

- PDF: PyPDF2, pdfplumber
- Word: python-docx
- Excel: openpyxl, pandas
- OCR: pytesseract
- NLP: spaCy, NLTK

## Notes

- Handle different encodings (UTF-8, Latin-1, etc.)
- Consider document size and memory usage
- Validate extracted data
- Handle malformed documents gracefully
""",
    "api-integration": """# {name}

{description}

## Overview

This skill integrates with external APIs to fetch and process data.

## Capabilities

- **HTTP Requests**: GET, POST, PUT, DELETE, PATCH
- **Authentication**: API keys, OAuth, JWT
- **Rate Limiting**: Respect API limits
- **Error Handling**: Retries, timeouts, fallbacks
- **Data Transformation**: Parse and format responses
- **Caching**: Reduce API calls

## Workflow

1. **Configure Connection**
   - Set API endpoint
   - Configure authentication
   - Set headers and parameters

2. **Make Request**
   - Build request URL
   - Add query parameters
   - Set request headers
   - Handle request body

3. **Handle Response**
   - Check status codes
   - Parse response (JSON, XML, etc.)
   - Extract relevant data
   - Handle pagination

4. **Error Handling**
   - Retry on failures
   - Implement exponential backoff
   - Log errors
   - Provide fallback data

5. **Transform Data**
   - Map API schema to internal schema
   - Validate data
   - Enrich with additional info

6. **Cache Results**
   - Cache successful responses
   - Set appropriate TTL
   - Invalidate stale data

## Example

```python
# Configure API client
api = APIClient(
    base_url="https://api.example.com/v1",
    api_key="your-api-key",
    timeout=30
)

# Make authenticated request
response = api.get(
    "/users",
    params={{"limit": 100, "page": 1}},
    headers={{"Accept": "application/json"}}
)

# Handle pagination
all_users = []
while response.has_next_page():
    users = response.json()["data"]
    all_users.extend(users)
    response = api.get(response.next_page_url())

# Transform data
transformed = [
    {{
        "id": user["id"],
        "name": user["full_name"],
        "email": user["email_address"]
    }}
    for user in all_users
]

# Cache results
cache.set("users_list", transformed, ttl=3600)
```

## Best Practices

- **Authentication**: Store credentials securely
- **Rate Limiting**: Respect API limits, implement backoff
- **Error Handling**: Handle network errors, timeouts, invalid responses
- **Logging**: Log requests and responses for debugging
- **Retries**: Implement retry logic with exponential backoff
- **Timeouts**: Set appropriate timeouts
- **Caching**: Cache responses when appropriate

## Common Patterns

### Retry with Exponential Backoff

```python
def make_request_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait_time = 2 ** attempt  # 1s, 2s, 4s
            time.sleep(wait_time)
```

### Pagination Handling

```python
def fetch_all_pages(api_url):
    results = []
    page = 1
    while True:
        response = api.get(f"{{api_url}}?page={{page}}")
        data = response.json()
        results.extend(data["items"])

        if not data["has_more"]:
            break
        page += 1

    return results
```

## Dependencies

- HTTP client: requests, httpx
- Authentication: requests-oauthlib
- Async support: aiohttp
- Rate limiting: ratelimit

## Notes

- Read API documentation carefully
- Test with API sandbox/staging first
- Monitor API usage and costs
- Handle API versioning
- Document API quirks and limitations
""",
}


class TemplateError(Exception):
    """Raised when template operations fail."""

    pass


def get_template(template_name: str, **kwargs: Any) -> str:
    """Get a skill template with variables substituted.

    Args:
        template_name: Name of template (basic, data-analysis, etc.)
        **kwargs: Variables to substitute in template (name, description, etc.)

    Returns:
        Template content with variables substituted

    Raises:
        TemplateError: If template not found or substitution fails

    Example:
        >>> content = get_template(
        ...     "basic",
        ...     name="my-skill",
        ...     description="My custom skill"
        ... )
    """
    if template_name not in TEMPLATES:
        available = ", ".join(TEMPLATES.keys())
        raise TemplateError(
            f"Template '{template_name}' not found. Available templates: {available}"
        )

    template = TEMPLATES[template_name]

    try:
        return template.format(**kwargs)
    except KeyError as e:
        raise TemplateError(f"Missing required variable {e} for template '{template_name}'") from e


def list_templates() -> list[str]:
    """List available template names.

    Returns:
        List of template names

    Example:
        >>> templates = list_templates()
        >>> print(templates)
        ['basic', 'data-analysis', 'code-generation', ...]
    """
    return list(TEMPLATES.keys())


def get_template_description(template_name: str) -> str:
    """Get a brief description of a template.

    Args:
        template_name: Name of template

    Returns:
        Template description

    Raises:
        TemplateError: If template not found

    Example:
        >>> desc = get_template_description("data-analysis")
        >>> print(desc)
        'Data processing and analysis'
    """
    descriptions = {
        "basic": "Simple skill template for general-purpose skills",
        "data-analysis": "Data processing, analysis, and visualization",
        "code-generation": "Code generation, modification, and refactoring",
        "document-processing": "Document parsing and information extraction",
        "api-integration": "External API integration and data fetching",
    }

    if template_name not in descriptions:
        raise TemplateError(f"Template '{template_name}' not found")

    return descriptions[template_name]
