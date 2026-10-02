from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"


@router.get("/")
def chat_page() -> FileResponse:
    """The browser chat UI. It only calls the public API (/auth/*, /chat,
    /ws/chat), so it doubles as a working example client."""
    return FileResponse(INDEX)
