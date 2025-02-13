"""This module contains functions for searching documents in the CozoDB based on embedding queries."""

import json
from typing import Any, Literal, TypeVar
from uuid import UUID

from beartype import beartype
from fastapi import HTTPException
from pycozo.client import QueryException
from pydantic import ValidationError

from ...autogen.openapi_model import DocReference
from ..utils import (
    cozo_query,
    partialclass,
    rewrap_exceptions,
    verify_developer_id_query,
    verify_developer_owns_resource_query,
    wrap_in_class,
)

ModelT = TypeVar("ModelT", bound=Any)
T = TypeVar("T")


@rewrap_exceptions(
    {
        QueryException: partialclass(HTTPException, status_code=400),
        ValidationError: partialclass(HTTPException, status_code=400),
        TypeError: partialclass(HTTPException, status_code=400),
    }
)
@wrap_in_class(
    DocReference,
    transform=lambda d: {
        "owner": {
            "id": d["owner_id"],
            "role": d["owner_type"],
        },
        "metadata": d.get("metadata", {}),
        **d,
    },
)
@cozo_query
@beartype
def search_docs_by_embedding(
    *,
    developer_id: UUID,
    owners: list[tuple[Literal["user", "agent"], UUID]],
    query_embedding: list[float],
    k: int = 3,
    confidence: float = 0.5,
    ef: int = 50,
    embedding_size: int = 1024,
    ann_threshold: int = 1_000_000,
    metadata_filter: dict[str, Any] = {},
) -> tuple[str, dict]:
    """
    Searches for document snippets in CozoDB by embedding query.

    Parameters:
        owner_type (Literal["user", "agent"]): The type of the owner of the documents.
        owner_id (UUID): The unique identifier of the owner.
        query_embedding (list[float]): The embedding vector of the query.
        k (int, optional): The number of nearest neighbors to retrieve. Defaults to 3.
        confidence (float, optional): The confidence threshold for filtering results. Defaults to 0.8.
        mmr_lambda (float, optional): The lambda parameter for MMR. Defaults to 0.25.
        embedding_size (int): Embedding vector length
        metadata_filter (dict[str, Any]): Dictionary to filter agents based on metadata.
    """

    assert len(query_embedding) == embedding_size
    assert sum(query_embedding)

    metadata_filter_str = ", ".join(
        [
            f"metadata->{json.dumps(k)} == {json.dumps(v)}"
            for k, v in metadata_filter.items()
        ]
    )

    owners: list[list[str]] = [
        [owner_type, str(owner_id)] for owner_type, owner_id in owners
    ]

    # Calculate the search radius based on confidence level
    radius: float = 1.0 - confidence

    determine_knn_ann_query = f"""
        owners[owner_type, owner_id] <- $owners
        snippet_counter[count(item)] :=
            owners[owner_type, owner_id_str],
            owner_id = to_uuid(owner_id_str),
            *docs:owner_id_metadata_doc_id_idx {{
                owner_type,
                owner_id,
                doc_id: item,
                metadata,
            }}
            {', ' + metadata_filter_str if metadata_filter_str.strip() else ''}

        ?[use_ann] := 
            snippet_counter[count],
            count > {ann_threshold},
            use_ann = true

        :limit 1
        :create _determine_knn_ann {{
            use_ann
        }}
    """

    # Construct the datalog query for searching document snippets
    search_query = f"""
        # %debug _determine_knn_ann
        %if {{ 
            ?[use_ann] := *_determine_knn_ann{{ use_ann }}
        }}

        %then {{
            owners[owner_type, owner_id] <- $owners
            input[
                owner_type,
                owner_id,
                query_embedding,
            ] :=
                owners[owner_type, owner_id_str],
                owner_id = to_uuid(owner_id_str),
                query_embedding = vec($query_embedding)

            # Search for documents by owner
            ?[
                doc_id,
                index,
                title,
                content,
                distance,
                embedding,
                metadata,
            ] :=
                # Get input values
                input[owner_type, owner_id, query],

                # Restrict the search to all documents that match the owner
                *docs:owner_id_metadata_doc_id_idx {{
                    owner_type,
                    owner_id,
                    doc_id,
                    metadata,
                }},
                *docs {{
                    doc_id,
                    title,
                }},

                # Search for snippets in the embedding space
                ~snippets:embedding_space {{
                    doc_id,
                    index,
                    content
                    |
                    query: query,
                    k: {k},
                    ef: {ef},
                    radius: {radius},
                    bind_distance: distance,
                    bind_vector: embedding,
                }}

            :sort distance
            :limit {k}

            :create _search_result {{
                doc_id,
                index,
                title,
                content,
                distance,
                embedding,
                metadata,
            }}
        }}

        %else {{
            owners[owner_type, owner_id] <- $owners
            input[
                owner_type,
                owner_id,
                query_embedding,
            ] :=
                owners[owner_type, owner_id_str],
                owner_id = to_uuid(owner_id_str),
                query_embedding = vec($query_embedding)

            # Search for documents by owner
            ?[
                doc_id,
                index,
                title,
                content,
                distance,
                embedding,
                metadata,
            ] :=
                # Get input values
                input[owner_type, owner_id, query],

                # Restrict the search to all documents that match the owner
                *docs {{
                    owner_type,
                    owner_id,
                    doc_id,
                    title,
                    metadata,
                }},

                # Search for snippets in the embedding space
                *snippets {{
                    doc_id,
                    index,
                    content,
                    embedding,
                }},
                !is_null(embedding),
                distance = cos_dist(query, embedding),
                distance <= {radius}

            :sort distance
            :limit {k}

            :create _search_result {{
                doc_id,
                index,
                title,
                content,
                distance,
                embedding,
                metadata,
            }}
        }}
        %end
    """

    normal_interim_query = f"""
        owners[owner_type, owner_id] <- $owners

        ?[
            owner_type,
            owner_id,
            doc_id,
            snippet_data,
            distance,
            title,
            embedding,
            metadata,
        ] := 
            owners[owner_type, owner_id_str],
            owner_id = to_uuid(owner_id_str),
            *_search_result{{ doc_id, index, title, content, distance, embedding, metadata }},
            snippet_data = [index, content]

        :sort distance
        :limit {k}

        :create _interim {{
            owner_type,
            owner_id,
            doc_id,
            snippet_data,
            distance,
            title,
            embedding,
            metadata,
        }}
    """

    collect_query = """
        n[
            doc_id,
            owner_type,
            owner_id,
            unique(snippet_data),
            distance,
            title,
            embedding,
            metadata,
        ] := 
            *_interim {
                owner_type,
                owner_id,
                doc_id,
                snippet_data,
                distance,
                title,
                embedding,
                metadata,
            }

        m[
            doc_id,
            owner_type,
            owner_id,
            snippet,
            distance,
            title,
            metadata,
        ] := 
            n[
                doc_id,
                owner_type,
                owner_id,
                snippet_data,
                distance,
                title,
                embedding,
                metadata,
            ],
            snippet = {
                "index": snippet_datum->0,
                "content": snippet_datum->1,
                "embedding": embedding,
            },
            snippet_datum in snippet_data

        ?[
            id,
            owner_type,
            owner_id,
            snippet,
            distance,
            title,
            metadata,
        ] := m[
            id,
            owner_type,
            owner_id,
            snippet,
            distance,
            title,
            metadata,
        ]

        :sort distance
    """

    verify_query = "}\n\n{".join(
        [
            verify_developer_id_query(developer_id),
            *[
                verify_developer_owns_resource_query(
                    developer_id, f"{owner_type}s", **{f"{owner_type}_id": owner_id}
                )
                for owner_type, owner_id in owners
            ],
        ]
    )

    query = f"""
        {{ {verify_query} }}
        {{ {determine_knn_ann_query} }}
        {search_query}
        {{ {normal_interim_query} }}
        {{ {collect_query} }}
    """

    return (
        query,
        {
            "owners": owners,
            "query_embedding": query_embedding,
        },
    )
