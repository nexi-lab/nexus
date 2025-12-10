# API Developer Guide

## Overview

This guide provides comprehensive documentation for integrating with the Nexus REST API. All endpoints follow RESTful conventions and return JSON responses.

**Base URL:** `https://api.nexus.example.com/v1`
**API Version:** v1.5.0
**Last Updated:** March 2024

## Authentication

All API requests (except public endpoints) require authentication via JWT tokens.

### Obtaining a Token

```http
POST /auth/login
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "secure_password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_in": 86400
}
```

### Token Usage

Include the access token in the Authorization header:

```http
GET /api/users/me
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
```

### Token Refresh

Access tokens expire after **24 hours**. Use the refresh token to obtain a new access token:

```http
POST /auth/refresh
Content-Type: application/json

{
  "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

## Rate Limiting

API requests are rate-limited to prevent abuse:

| User Type | Limit | Window |
|-----------|-------|--------|
| Anonymous | 100 requests | 1 minute |
| Authenticated | 1000 requests | 1 minute |
| Admin | 5000 requests | 1 minute |

Rate limit headers are included in all responses:
- `X-RateLimit-Limit`: Maximum requests allowed
- `X-RateLimit-Remaining`: Requests remaining
- `X-RateLimit-Reset`: Unix timestamp when limit resets

## Pagination

List endpoints support pagination via query parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| page | integer | 1 | Page number (1-indexed) |
| limit | integer | 20 | Items per page (max: 100) |

**Example:**
```http
GET /api/projects?page=2&limit=25
```

**Response includes pagination metadata:**
```json
{
  "data": [...],
  "pagination": {
    "page": 2,
    "limit": 25,
    "total": 150,
    "total_pages": 6
  }
}
```

## Error Handling

All errors follow a consistent format:

```json
{
  "success": false,
  "error": "Descriptive error message",
  "error_code": 400,
  "request_id": "abc123-def456"
}
```

### Common Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad Request - Invalid input |
| 401 | Unauthorized - Invalid or missing token |
| 403 | Forbidden - Insufficient permissions |
| 404 | Not Found - Resource doesn't exist |
| 429 | Too Many Requests - Rate limit exceeded |
| 500 | Internal Server Error |

## Endpoints

### Users

#### List Users (Admin only)
```http
GET /api/users
```

#### Get Current User
```http
GET /api/users/me
```

#### Update User Profile
```http
PUT /api/users/me
Content-Type: application/json

{
  "name": "New Name",
  "avatar_url": "https://..."
}
```

### Projects

#### List Projects
```http
GET /api/projects
```

Query parameters:
- `status`: Filter by status (active, archived, all)
- `owner_id`: Filter by owner

#### Create Project
```http
POST /api/projects
Content-Type: application/json

{
  "name": "My Project",
  "description": "Project description"
}
```

### Files

#### Upload File
```http
POST /api/files/upload
Content-Type: multipart/form-data

file: <binary data>
project_id: "project-uuid"
```

**Size Limit:** 50MB
**Allowed Types:** pdf, doc, docx, xls, xlsx, png, jpg, gif

#### Download File
```http
GET /api/files/{file_id}/download
```

## Webhooks

Configure webhooks to receive real-time notifications for events.

### Supported Events

- `project.created`
- `project.updated`
- `project.deleted`
- `file.uploaded`
- `user.invited`

### Webhook Payload

```json
{
  "event": "project.created",
  "timestamp": "2024-03-15T10:30:00Z",
  "data": {
    "project_id": "uuid",
    "name": "Project Name"
  }
}
```

## SDKs

Official SDKs are available for:

- **Python**: `pip install nexus-sdk`
- **JavaScript**: `npm install @nexus/sdk`
- **Go**: `go get github.com/nexus/go-sdk`

## Support

For API support, contact: api-support@nexus.example.com
