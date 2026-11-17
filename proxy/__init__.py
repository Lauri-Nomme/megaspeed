"""Development-only tooling: the Cloud Jira emulation proxy (prd/24).

Nothing here ships in the scraper artifact and nothing here is imported by the
ingest or the server: it is the local stand-in for Atlassian Cloud's SaaS API.

    python3 -m proxy.cloud_jira_proxy --upstream http://127.0.0.1:8090 \\
            --port 8091 --pat cloud-lab-pat \\
            --user admin --password admin-lab-pw
"""

__all__: list[str] = []
