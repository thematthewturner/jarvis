# Todoist API Developer Guide

Welcome to the quick-start developer guide for the **Todoist API**. This guide covers the essential concepts, authentication methods, and core functionalities you need to know when building integrations or applications with Todoist.

## 1. Overview: REST vs. Sync API

Todoist provides two main ways to interact with its data:

* **REST API:** A standard, conventional RESTful API ideal for simple integrations, single requests, and basic CRUD (Create, Read, Update, Delete) operations.
* **Sync API (`/sync`):** A specialized endpoint designed for first-party and complex third-party clients. It allows you to batch multiple commands in a single request and sync data incrementally. This is highly recommended if your app needs offline support or needs to process large amounts of data efficiently.

## 2. Authentication & Authorization

All API requests require authentication. Depending on your use case, there are two primary ways to authenticate:

### Personal API Token (For personal scripts/apps)

If you are building an app just for yourself, you can use your Personal API token.

* **Usage:** Pass it in the header of your HTTP requests.
* **Header format:** `Authorization: Bearer YOUR_API_TOKEN`

### OAuth 2.0 (For third-party integrations)

If you are building an application for other Todoist users, you must use OAuth.

1. **Authorization Request:** Redirect the user to `https://app.todoist.com/oauth/authorize` with your `client_id` and requested `scope` (e.g., `data:read`, `task:add`, `data:read_write`).
2. **Redirection:** The user authorizes the app, and Todoist redirects them back to your site with an authorization `code`.
3. **Token Exchange:** Exchange the `code` for an `access_token` by making a POST request to `https://api.todoist.com/oauth/access_token`.

## 3. Official SDKs

To speed up development, Todoist officially maintains SDKs for popular languages:

* **Todoist Python SDK:** Easily installable via `pip`.
* **Todoist TypeScript/JavaScript SDK:** Available on npm, fully typed for seamless integration.

## 4. Key Resources

The Todoist API revolves around several core objects. Here are the main ones you will interact with:

* **Projects:** The main containers for tasks. You can fetch, create, update, and archive projects.
* **Tasks (Items):** The core to-do items. You can set due dates, priorities, and assignments.
* **Sections:** Used to divide projects into smaller, organized parts (often used for Kanban boards).
* **Comments & Attachments:** Add context to tasks or projects, including file uploads.
* **Labels & Filters:** For organizing and querying tasks efficiently.

## 5. Webhooks

Instead of constantly polling the API for changes, you can use **Webhooks**. You can configure a webhook URL in your App Management Console. Todoist will send HTTP POST payloads to your URL whenever a user creates, completes, or updates an item.

## 6. Pagination and Limits

* **Rate Limits:** Keep an eye on the API rate limits to prevent your app from being temporarily blocked. Batch your requests using the Sync API whenever possible.
* **Pagination:** Endpoints returning lists of items (like Activity Logs or Completed Tasks) use cursor-based pagination. Use the provided parameters (like `limit` and `offset` or sync tokens) to fetch large datasets incrementally.

## 7. Cross-Origin Resource Sharing (CORS)

The Todoist API fully supports CORS. This means you can securely make API requests directly from a user's web browser in a client-side application (like React or Vue) without needing a backend proxy, provided you pass the correct Bearer token.

---

**Helpful Links:**

* **Full Documentation:** [https://developer.todoist.com/api/v1/](https://developer.todoist.com/api/v1/)
* **App Management Console:** Where you register your OAuth applications and configure Webhooks.