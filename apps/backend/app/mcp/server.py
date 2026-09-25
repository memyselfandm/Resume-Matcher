"""MCP server factory.

``build_mcp_server`` returns a fresh ``MCPServer`` for every app lifespan: the
Streamable HTTP session manager is single-use, so transports must never share
a server instance across lifespans.
"""

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer

from app import __version__
from app.mcp import resources
from app.mcp.runtime import MCPRuntime
from app.mcp.tools import ats, documents, jobs, resumes, system, tailoring, tracker

SERVER_NAME = "resume-matcher"
SERVER_INSTRUCTIONS = (
    "Resume Matcher tailors resumes to job descriptions and tracks applications. "
    "Typical flow: upload_resume -> add_jobs -> tailor_resume_preview -> "
    "tailor_resume_confirm (creates the tailored resume and a tracker card) -> "
    "export_resume_pdf -> update_application. tailor_and_verify runs preview, "
    "confirm and an ATS parse check of the saved result in one call; "
    "ats_parse_check_file and ats_parse_check_resume check readability by an "
    "ATS. Long operations return "
    "status=running with a task_id; poll get_task. Call get_status first to "
    "check LLM and PDF readiness."
)

# The tool surface only changes with a release; resource contents change with
# every edit, so reads are never cached.
TOOLS_LIST_CACHE_HINT = CacheHint(ttl_ms=3_600_000, scope="private")
RESOURCE_READ_CACHE_HINT = CacheHint(ttl_ms=0, scope="private")


def build_mcp_server(runtime: MCPRuntime) -> MCPServer:
    """Create an MCP server exposing Resume Matcher's agent tools.

    Tools are registered in a fixed order, which ``tools/list`` preserves.
    Deletes, configuration writes and API-key routes are intentionally not
    exposed.
    """
    server: MCPServer = MCPServer(
        SERVER_NAME,
        title="Resume Matcher",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        cache_hints={
            "tools/list": TOOLS_LIST_CACHE_HINT,
            "resources/read": RESOURCE_READ_CACHE_HINT,
        },
    )
    for group in (system, resumes, jobs, tailoring, documents, ats, tracker):
        group.register(server, runtime)
    system.register_task_tools(server, runtime)
    resources.register(server, runtime)
    return server
