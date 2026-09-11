from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.api.auth import require_internal_token
from x_digest.api.errors import APIError
from x_digest.api.schemas import ListCreate, ListResponse, ListUpdate
from x_digest.models import PostListMembership, XList

router = APIRouter(prefix="/api/lists", dependencies=[Depends(require_internal_token)])


def _session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


class ListService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list(self) -> list[XList]:
        return list(self._session.scalars(select(XList).order_by(XList.created_at, XList.id)))

    def create(self, payload: ListCreate) -> XList:
        x_list = XList(**payload.model_dump())
        self._session.add(x_list)
        try:
            self._session.commit()
        except IntegrityError as error:
            self._session.rollback()
            raise APIError(409, "list_already_exists", "List already exists") from error
        self._session.refresh(x_list)
        return x_list

    def update(self, list_id: str, payload: ListUpdate) -> XList:
        x_list = self._required(list_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(x_list, field, value)
        self._session.commit()
        self._session.refresh(x_list)
        return x_list

    def delete(self, list_id: str) -> None:
        x_list = self._required(list_id)
        self._session.execute(
            delete(PostListMembership).where(PostListMembership.list_id == list_id)
        )
        self._session.delete(x_list)
        self._session.commit()

    def _required(self, list_id: str) -> XList:
        x_list = self._session.get(XList, list_id)
        if x_list is None:
            raise APIError(404, "list_not_found", "List not found")
        return x_list


@router.get("", response_model=list[ListResponse])
def list_lists(session: Session = Depends(_session)) -> list[XList]:
    return ListService(session).list()


@router.post("", response_model=ListResponse, status_code=status.HTTP_201_CREATED)
def create_list(payload: ListCreate, session: Session = Depends(_session)) -> XList:
    return ListService(session).create(payload)


@router.patch("/{list_id}", response_model=ListResponse)
def update_list(list_id: str, payload: ListUpdate, session: Session = Depends(_session)) -> XList:
    return ListService(session).update(list_id, payload)


@router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_list(list_id: str, session: Session = Depends(_session)) -> Response:
    ListService(session).delete(list_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
