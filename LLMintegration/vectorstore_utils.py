"""
What it does:
0. Initializes the HuggingFace embedding model and Chroma vector store once.
1. Get the tokenizer from the embedding model's client (if available), or fallback to AutoTokenizer, or fallback to character-based splitting without any tokenizer.
2. Split the text content into chunks using Recursive Splitter based on the tokenizer/character length.
3. Add each chunk to the vector store with metadata linking it to the original document ID (i.e. post_32) and chunk index (2 meaning 3rd chunk). To uniquely identify chunks in the ChromaDB, we combine them. (e.g., post_32_chunk_2).
4. Implemented a delete function that removes ALL chunks associated with a given original document ID (i.e. post_32).
5. Implemented a search function that retrieves chunks based on a query with all metadata intact, allowing you to trace back to the original document and chunk index/sequence.
"""

import logging

from django.conf import settings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings

logger = logging.getLogger(__name__)

_embedding_function = None
_vector_store = None

# --- Configuration for Chunking ---

MAX_TOKENS = 2048
FALLBACK_CHUNK_SIZE_CHARS = 6144  # if 1 token ~ 3 chars, 2048 tokens ~ 6144 chars.
FALLBACK_CHUNK_OVERLAP_CHARS = max(
    0, FALLBACK_CHUNK_SIZE_CHARS // 5
)  # e.g., ~20% overlap, ensure non-negative # characters
# Metadata keys for linking chunks to original documents
ORIGINAL_DOC_ID_KEY = "original_doc_id"
CHUNK_INDEX_KEY = "chunk_index"


def get_embedding_function():
    """Initializes and returns the Google Generative AI embedding function."""
    global _embedding_function
    if _embedding_function is None:
        logger.info("Initializing Google Generative AI embedding model.")
        _embedding_function = GoogleGenerativeAIEmbeddings(
            # model="text-multilingual-embedding-002",
            model="models/embedding-001",
            google_api_key=settings.EMBEDDING_API_KEY,
        )
        logger.info("Google Generative AI embedding model initialized.")
    return _embedding_function


def get_vector_store():
    """Initializes and returns the Chroma vector store."""
    global _vector_store
    if _vector_store is None:
        embedding_function = get_embedding_function()
        logger.info(
            f"Initializing Chroma vector store at: {settings.CHROMA_PERSIST_DIRECTORY}"
        )
        _vector_store = Chroma(
            collection_name="dormitory_content",  # Consider making this configurable via settings
            embedding_function=embedding_function,
            persist_directory=str(settings.CHROMA_PERSIST_DIRECTORY),
        )
        logger.info("Chroma vector store initialized.")
    return _vector_store


def _get_text_splitter():
    """
    Initializes and returns a character-based text splitter.
    """
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=FALLBACK_CHUNK_SIZE_CHARS,
        chunk_overlap=FALLBACK_CHUNK_OVERLAP_CHARS,
        length_function=len,
        is_separator_regex=False,
    )
    logger.info(
        f"Using character-based splitting. Chunk size: {FALLBACK_CHUNK_SIZE_CHARS} chars, "
        f"Overlap: {FALLBACK_CHUNK_OVERLAP_CHARS} chars."
    )
    return text_splitter


def add_document_to_vectorstore(
    original_doc_id: str, text_content: str, metadata: dict
):  # like post_32, text_content, metadata dict containing source_type, document_id, author_username, created_at, url
    """
    Splits a document into chunks and adds them to the vector store.
    Each chunk is associated with the original document ID and includes original metadata.
    """
    try:
        if not text_content or not text_content.strip():
            logger.warning(
                f"Text content for document {original_doc_id} is empty or whitespace only. Skipping."
            )
            return

        vector_store = get_vector_store()
        text_splitter = _get_text_splitter()

        text_chunks = text_splitter.split_text(text_content)

        if not text_chunks:
            logger.warning(
                f"No text chunks generated for document {original_doc_id} (original_doc_id). Content might be too short after splitting attempt."
            )
            return

        documents_to_add = []
        chunk_ids_for_db = []

        for i, chunk_text in enumerate(text_chunks):
            # Create a unique ID for each chunk to store in Chroma
            chunk_db_id = f"{original_doc_id}_chunk_{i}"

            # Combine original metadata with chunk-specific metadata
            chunk_metadata = {
                **metadata,  # Original metadata passed to the function
                ORIGINAL_DOC_ID_KEY: original_doc_id,  # Link to the parent document # this will be used to trace back to the original document, and while deleting
                CHUNK_INDEX_KEY: i,  # Sequence of the chunk
            }

            doc = Document(page_content=chunk_text, metadata=chunk_metadata)
            documents_to_add.append(doc)
            chunk_ids_for_db.append(chunk_db_id)

        if documents_to_add:
            vector_store.add_documents(documents=documents_to_add, ids=chunk_ids_for_db)
            logger.info(
                f"{len(documents_to_add)} chunks for document {original_doc_id} added to vector store."
            )
        else:
            # This case should ideally be caught by 'if not text_chunks' earlier
            logger.info(
                f"No document chunks to add for {original_doc_id} after processing."
            )

    except Exception as e:
        logger.error(
            f"Error adding document {original_doc_id} (chunked): {e}", exc_info=True
        )


def delete_document_from_vectorstore(original_doc_id: str):
    """
    Deletes all chunks associated with the given original document ID from the vector store.
    """
    try:
        vector_store = get_vector_store()

        # Chroma's delete method can use a 'where' filter based on metadata.
        # This will delete all chunks that have 'original_doc_id' set to the provided ID.
        filter_criteria = {ORIGINAL_DOC_ID_KEY: original_doc_id}

        # The Langchain Chroma wrapper passes `where` to the underlying Chroma client's delete method.
        vector_store.delete(where=filter_criteria)

        logger.info(
            f"Attempted deletion of all chunks for document {original_doc_id} (matching filter {filter_criteria}) from vector store."
        )

    except Exception as e:
        # Log error, but don't necessarily re-raise if, e.g., the document wasn't there.
        # Chroma's delete with a `where` clause typically doesn't error if no documents match.
        logger.error(
            f"Error deleting document chunks for {original_doc_id}: {e}", exc_info=True
        )


def search_vectorstore(
    query: str, k: int = 5, fetch_k: int = 10, lambda_mult: float = 0.5
):  # k means how many chunks to return, fetch_k is how many chunks to fetch before MMR filtering, lambda_mult is the trade-off between relevance and diversity (1.0 means only relevance, 0.0 means only diversity, default is 0.5 which balances both)
    """
    Searches the vector store using Maximal Marginal Relevance (MMR) search.
    Returns diverse, relevant document chunks.
    The metadata of each chunk contains ORIGINAL_DOC_ID_KEY and CHUNK_INDEX_KEY
    to link back to the original post/comment.
    """
    try:
        vector_store = get_vector_store()
        logger.info(
            f"Searching vector store with MMR for query"  #: '{query}', k={k}, fetch_k={fetch_k}, lambda_mult={lambda_mult}"
        )

        # Perform MMR search. Results will be Document objects representing chunks.
        results = vector_store.max_marginal_relevance_search(
            query=query, k=k, fetch_k=fetch_k, lambda_mult=lambda_mult
        )

        # print("RESULTS PRINTING--------------------------------")
        # print(results)
        # print("RESULTS ENDED--------------------------------")
        # Example of results:
        # [Document(id='post_14_chunk_0', metadata={'original_doc_id': 'post_14', 'source_type': 'post', 'url': '/posts/14/', 'created_at': '2025-06-05', 'document_id': '14', 'author_username': 'string', 'chunk_index': 0, 'title': 'joined bdapps seminar today'}, page_content='Post Title: joined bdapps seminar today\nPost Content: dammm! this competition will really be impactful for my CV. it is supported by ROBI and offers 100k prizepool.'), Document(id='post_13_chunk_0', metadata={'document_id': '13', 'created_at': '2025-06-05', 'url': '/posts/13/', 'source_type': 'post', 'author_username': 'string2', 'title': 'BDAPPS competition!', 'original_doc_id': 'post_13', 'chunk_index': 0}, page_content='Post Title: BDAPPS competition!\nPost Content: BDAPPS summit deadline is extended to June 2025! Hurry up now! Work on your ideas... I am submitting a project in Django. Anyone interested to collaborate? I am looking for mates who knows REACT for frontend!'), Document(id='post_10', metadata={'url': '/posts/10/', 'source_type': 'post', 'author_username': 'string', 'document_id': '10', 'created_at': '2025-06-04', 'title': 'got a mocka pot'}, page_content='Post Title: got a mocka pot\nPost Content: its only 2000 BDT. but its too small for 10 people')]
        logger.info(
            f"Found {len(results)} document chunks for query."  # they are: '{results}'"
        )
        # Example of accessing original document ID from a retrieved chunk:
        # if results:
        #     first_chunk = results[0]
        #     original_id = first_chunk.metadata.get(ORIGINAL_DOC_ID_KEY)
        #     chunk_idx = first_chunk.metadata.get(CHUNK_INDEX_KEY)
        #     logger.debug(f"First retrieved chunk belongs to original doc '{original_id}', chunk index {chunk_idx}")
        return results
    except Exception as e:
        logger.error(
            f"Error searching vector store for query '{query}': {e}", exc_info=True
        )
        return []


def semantic_search(query: str, limit: int = 20, offset: int = 0):
    """
    Searches the vector store using similarity search with pagination.
    Returns a list of unique Post IDs for the most similar documents for the given page,
    ordered by relevance.

    Args:
        query (str): The search query.
        limit (int): The maximum number of Post IDs to return for the current page (default: 20).
        offset (int): The starting index of Post IDs to retrieve for pagination (default: 0).
    """
    try:
        vector_store = get_vector_store()

        # Calculate the total number of chunks to fetch to cover the desired page.
        # This needs to be large enough to potentially find 'limit' unique post_ids
        # after processing. We might fetch more chunks than 'limit' post_ids.
        # A simple heuristic: fetch 3-5 times the number of desired post_ids,
        # as multiple chunks might belong to the same post or be irrelevant.
        # This value might need tuning based on typical data distribution.
        fetch_k_chunks = (
            offset + limit
        ) * 5  # Fetch more chunks to ensure enough unique posts

        logger.info(
            f"Searching vector store with similarity search for query: '{query}', "
            f"fetching up to {fetch_k_chunks} chunks for pagination (target offset={offset}, target limit={limit} post IDs)"
        )

        # Perform similarity search to get enough document chunks
        all_results_chunks = vector_store.similarity_search(
            query=query, k=fetch_k_chunks
        )

        logger.info(
            f"Fetched {len(all_results_chunks)} document chunks for query: '{query}'."
        )

        # Extract unique Post IDs from the chunks, preserving order of first appearance
        unique_post_ids = []
        seen_post_ids = set()

        for chunk in all_results_chunks:
            if not chunk.metadata:
                logger.warning(
                    f"Document chunk {chunk.id if hasattr(chunk, 'id') else 'unknown'} has no metadata."
                )
                continue

            original_doc_id = chunk.metadata.get(ORIGINAL_DOC_ID_KEY)
            post_id = None

            try:
                if original_doc_id and original_doc_id.startswith("post_"):
                    post_id = int(original_doc_id.split("_")[1])
                elif original_doc_id and (
                    original_doc_id.startswith("comment_")
                    or original_doc_id.startswith("reply_")
                ):
                    # For comments/replies, the metadata should contain the parent post_id
                    post_id_str = chunk.metadata.get("post_id")
                    if post_id_str:
                        post_id = int(post_id_str)
                    else:
                        logger.warning(
                            f"Chunk {original_doc_id} is a comment/reply but lacks 'post_id' in metadata."
                        )
                else:
                    logger.warning(
                        f"Chunk {original_doc_id or 'unknown'} has an unrecognized original_doc_id format."
                    )
                    continue

                if post_id and post_id not in seen_post_ids:
                    unique_post_ids.append(post_id)
                    seen_post_ids.add(post_id)

            except ValueError:
                logger.warning(
                    f"Could not parse post ID from original_doc_id: '{original_doc_id}' or metadata post_id: '{chunk.metadata.get('post_id')}'"
                )
                continue
            except Exception as e:
                logger.error(
                    f"Unexpected error processing chunk {original_doc_id}: {e}",
                    exc_info=True,
                )
                continue

        # Apply pagination to the list of unique Post IDs
        paginated_post_ids = unique_post_ids[offset : offset + limit]

        logger.info(
            f"Returning {len(paginated_post_ids)} unique Post IDs for the current page "
            f"(offset={offset}, limit={limit}) from {len(unique_post_ids)} total unique post IDs found. IDs: {paginated_post_ids}"
        )
        return paginated_post_ids

    except Exception as e:
        logger.error(
            f"Error performing paginated similarity search for query '{query}': {e}",
            exc_info=True,
        )
        return []


def search_by_vector(
    embedding_vector: list[float],
    k: int = 10,
    fetch_k: int = 20,
    lambda_mult: float = 0.75,
    use_mmr: bool = False,
):
    """
    Searches the vector store for documents similar to a given embedding vector using MMR.

    Args:
        embedding_vector (list[float]): The embedding vector to search with.
        k (int): The final number of documents to return.
        fetch_k (int): The number of documents to fetch before MMR reranking.
        lambda_mult (float): Diversity vs. relevance factor (1.0 = relevance, 0.0 = diversity).

    Returns:
        list[Document]: A list of diverse, relevant document chunks.
    """
    try:
        vector_store = get_vector_store()
        logger.info(
            f"Searching vector store with MMR by vector, k={k}, fetch_k={fetch_k}"
        )

        if use_mmr:
            results = vector_store.max_marginal_relevance_search_by_vector(
                embedding=embedding_vector,
                k=k,
                fetch_k=fetch_k,
                lambda_mult=lambda_mult,
            )
        else:
            results = vector_store.similarity_search_by_vector(
                embedding=embedding_vector, k=k
            )

        logger.info(f"Found {len(results)} document chunks by vector search.")
        return results
    except Exception as e:
        logger.error(f"Error searching vector store by vector: {e}", exc_info=True)
        return []


'''
Old code without chunking, for reference:
# import logging

# from django.conf import settings
# from langchain_chroma import Chroma
# from langchain_core.documents import Document
# from langchain_huggingface import HuggingFaceEmbeddings

# logger = logging.getLogger(__name__)

# _embedding_function = None
# _vector_store = None


# def get_embedding_function():
#     """Initializes and returns the HuggingFace embedding function."""
#     global _embedding_function
#     if _embedding_function is None:
#         logger.info(f"Initializing embedding model: {settings.EMBEDDING_MODEL_NAME}")
#         _embedding_function = HuggingFaceEmbeddings(
#             model_name=settings.EMBEDDING_MODEL_NAME,
#             model_kwargs={"device": "cpu"},
#             encode_kwargs={"normalize_embeddings": True},  # Cosine similarity
#         )
#         logger.info("Embedding model initialized.")
#     return _embedding_function


# def get_vector_store():
#     """Initializes and returns the Chroma vector store."""
#     global _vector_store
#     if _vector_store is None:
#         embedding_function = get_embedding_function()
#         logger.info(
#             f"Initializing Chroma vector store at: {settings.CHROMA_PERSIST_DIRECTORY}"
#         )
#         _vector_store = Chroma(
#             collection_name="dormitory_content",
#             embedding_function=embedding_function,
#             persist_directory=str(settings.CHROMA_PERSIST_DIRECTORY),
#         )
#         logger.info("Chroma vector store initialized.")
#     return _vector_store


# def add_document_to_vectorstore(doc_id: str, text_content: str, metadata: dict):
#     """Adds a single document to the vector store."""
#     try:
#         vector_store = get_vector_store()
#         document = Document(page_content=text_content, metadata=metadata)
#         vector_store.add_documents(documents=[document], ids=[doc_id])
#         logger.info(f"Document {doc_id} added to vector store.")
#     except Exception as e:
#         logger.error(f"Error adding document {doc_id}: {e}", exc_info=True)


# def delete_document_from_vectorstore(doc_id: str):
#     """Deletes a document from the vector store by its ID."""
#     try:
#         vector_store = get_vector_store()
#         vector_store.delete(ids=[doc_id])
#         logger.info(f"Document {doc_id} deleted from vector store.")
#     except Exception as e:
#         logger.error(f"Error deleting document {doc_id}: {e}", exc_info=True)


# def search_vectorstore(
#     query: str, k: int = 5, fetch_k: int = 10, lambda_mult: float = 0.5
# ):
#     """
#     Searches the vector store using Maximal Marginal Relevance (MMR) search.
#     Returns diverse, relevant documents.

#     Args:
#         query (str): The search query.
#         k (int): Number of documents to return (default: 5).
#         fetch_k (int): Number of initial documents to fetch for MMR (default: 10).
#         lambda_mult (float): MMR trade-off between relevance (1.0) and diversity (0.0, default: 0.5).

#     Returns:
#         List[Document]: List of relevant, diverse documents.
#     """
#     try:
#         vector_store = get_vector_store()
#         logger.info(
#             f"Searching vector store with MMR for query: '{query}', k={k}, fetch_k={fetch_k}, lambda_mult={lambda_mult}"
#         )

#         # Perform MMR search
#         results = vector_store.max_marginal_relevance_search(
#             query=query, k=k, fetch_k=fetch_k, lambda_mult=lambda_mult
#         )

#         logger.info(f"Found {len(results)} documents for query: '{query}'")
#         return results
#     except Exception as e:
#         logger.error(
#             f"Error searching vector store for query '{query}': {e}", exc_info=True
#         )
#         return []
# '''
