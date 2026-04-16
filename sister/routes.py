"""FastAPI route handler functions for sister.

These are plain async functions that accept their dependencies as arguments.
They are registered with FastAPI decorators in main.py.
"""

import logging
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from .database import count_responses, find_responses
from .models import (
    AuthenticationError,
    ElencoImmobiliInput,
    ElencoImmobiliRequest,
    GenericSisterRequest,
    IspezioneIpotecariaInput,
    IspezioneIpotecariaRequest,
    QueueFullError,
    SezioniExtractionRequest,
    VisuraInput,
    VisuraIntestatiInput,
    VisuraIntestatiRequest,
    VisuraPersonaGiuridicaInput,
    VisuraPersonaGiuridicaRequest,
    VisuraRequest,
    VisuraSoggettoInput,
    VisuraSoggettoRequest,
    WorkflowInput,
)
from .models import SubmitResult
from .services import VisuraService

logger = logging.getLogger("sister")


def _submit_result_to_response(results: list, tipos_catasto: list, message: str):
    """Convert SubmitResult list to JSONResponse, handling cached results."""
    any_cached = any(isinstance(r, SubmitResult) and r.cached for r in results)
    request_ids = []
    cached_data = []
    for r in results:
        if isinstance(r, SubmitResult):
            request_ids.append(r.request_id)
            if r.cached and r.response:
                cached_data.append({
                    "request_id": r.request_id,
                    "tipo_catasto": r.response.tipo_catasto,
                    "status": "completed" if r.response.success else "error",
                    "data": r.response.data,
                    "error": r.response.error,
                })
        elif isinstance(r, str):
            request_ids.append(r)

    resp: dict = {
        "request_ids": request_ids,
        "tipos_catasto": tipos_catasto,
        "status": "cached" if any_cached and len(cached_data) == len(results) else "queued",
        "message": message,
    }
    if cached_data:
        resp["cached_results"] = cached_data
    return JSONResponse(resp)


async def richiedi_visura(request: VisuraInput, service: VisuraService, force: bool = False):
    """Richiede una visura catastale fornendo direttamente i dati catastali"""
    try:
        sezione = None if request.sezione == "_" else request.sezione

        tipos_catasto = [request.tipo_catasto] if request.tipo_catasto else ["T", "F"]
        visura_requests = []
        for tipo_catasto in tipos_catasto:
            request_id = f"req_{tipo_catasto}_{uuid4().hex}"
            visura_requests.append(
                VisuraRequest(
                    request_id=request_id,
                    tipo_catasto=tipo_catasto,
                    provincia=request.provincia,
                    comune=request.comune,
                    sezione=sezione,
                    sezione_urbana=request.sezione_urbana,
                    foglio=request.foglio,
                    particella=request.particella,
                    subalterno=request.subalterno,
                )
            )
        results = await service.add_requests_batch(visura_requests, force=force)

        return _submit_result_to_response(
            results, tipos_catasto,
            f"Richieste per {request.comune} F.{request.foglio} P.{request.particella}",
        )

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Errore nella richiesta visura: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def ottieni_visura(request_id: str, service: VisuraService):
    """Ottiene il risultato di una visura"""
    try:
        response = await service.get_response(request_id)

        if response is None:
            request_state = service.get_request_state(request_id)

            if request_state == "processing":
                return JSONResponse(
                    {"request_id": request_id, "status": "processing", "message": "Richiesta in elaborazione"}
                )

            if request_state == "expired":
                return JSONResponse(
                    {
                        "request_id": request_id,
                        "status": "expired",
                        "message": "Risultato non più disponibile (cache scaduta)",
                    },
                    status_code=410,
                )

            raise HTTPException(status_code=404, detail="request_id non trovato")

        return JSONResponse(
            {
                "request_id": request_id,
                "tipo_catasto": response.tipo_catasto,
                "status": "completed" if response.success else "error",
                "data": response.data,
                "error": response.error,
                "timestamp": response.timestamp.isoformat(),
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Errore nell'ottenere visura: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def richiedi_intestati_immobile(request: VisuraIntestatiInput, service: VisuraService, force: bool = False):
    """Richiede gli intestati per un immobile specifico."""
    try:
        sezione = None if request.sezione == "_" else request.sezione
        tipo_catasto = request.tipo_catasto or "T"

        request_id = f"intestati_{tipo_catasto}_{uuid4().hex}"

        intestati_request = VisuraIntestatiRequest(
            request_id=request_id,
            tipo_catasto=tipo_catasto,
            provincia=request.provincia,
            comune=request.comune,
            foglio=request.foglio,
            particella=request.particella,
            subalterno=request.subalterno,
            sezione=sezione,
            sezione_urbana=request.sezione_urbana,
        )

        result = await service.add_intestati_request(intestati_request, force=force)

        if hasattr(result, "cached") and result.cached and result.response:
            resp = result.response
            return JSONResponse({
                "request_id": result.request_id,
                "tipo_catasto": tipo_catasto,
                "status": "completed" if resp.success else "error",
                "data": resp.data,
                "error": resp.error,
                "timestamp": resp.timestamp.isoformat() if resp.timestamp else None,
            })

        return JSONResponse(
            {
                "request_id": request_id,
                "tipo_catasto": tipo_catasto,
                "subalterno": request.subalterno,
                "status": "queued",
                "message": f"Richiesta intestati aggiunta alla coda per {request.comune} F.{request.foglio} P.{request.particella}",
                "queue_position": service.request_queue.qsize(),
            }
        )

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Errore nella richiesta intestati: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def health_check(service: VisuraService):
    """Controlla lo stato del servizio"""
    db_stats = await count_responses()
    return JSONResponse(
        {
            "status": "healthy" if service.auth_ready else "degraded",
            "auth": service.auth_status,
            "auth_ready": service.auth_ready,
            "authenticated": service.browser_manager.authenticated,
            "queue_size": service.request_queue.qsize(),
            "pending_requests": len(service.pending_request_ids),
            "cached_responses": len(service.response_store),
            "response_ttl_seconds": service.response_ttl_seconds,
            "response_max_items": service.response_max_items,
            "queue_max_size": service.queue_max_size,
            "response_cleanup_interval_seconds": service.response_cleanup_interval_seconds,
            "database": db_stats,
        }
    )


async def visura_history(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Cerca nello storico delle visure salvate nel database."""
    results = await find_responses(
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        limit=min(limit, 200),
        offset=offset,
    )
    return JSONResponse({"count": len(results), "results": results})


async def graceful_shutdown_endpoint(service: VisuraService):
    """Effettua uno shutdown graceful del servizio"""
    try:
        logger.info("Shutdown graceful richiesto via API")
        await service.graceful_shutdown()
        return JSONResponse({"status": "success", "message": "Shutdown graceful completato"})
    except Exception as e:
        logger.error("Errore durante shutdown graceful via API: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def extract_sezioni(request: SezioniExtractionRequest, service: VisuraService):
    """
    Estrae le sezioni territoriali d'Italia per il tipo catasto specificato.
    ATTENZIONE: Questa operazione può richiedere diverse ore!
    I dati vengono restituiti nella risposta.
    """
    try:
        logger.info(
            "Iniziando estrazione sezioni per tipo catasto: %s, max province: %s",
            request.tipo_catasto,
            request.max_province,
        )

        sezioni_data = await service.browser_manager.esegui_extract_sezioni(request.tipo_catasto, request.max_province)

        if not sezioni_data:
            return JSONResponse({"status": "no_data", "message": "Nessuna sezione estratta", "count": 0})

        logger.info("Estrazione sezioni completata: %d totali", len(sezioni_data))

        return JSONResponse(
            {
                "status": "success",
                "message": f"Estrazione completata per tipo catasto {request.tipo_catasto}",
                "total_extracted": len(sezioni_data),
                "tipo_catasto": request.tipo_catasto,
                "sezioni": sezioni_data,
            }
        )

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Errore durante estrazione sezioni: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def richiedi_visura_soggetto(request: VisuraSoggettoInput, service: VisuraService, force: bool = False):
    """Ricerca per soggetto (codice fiscale) — ambito nazionale o provinciale."""
    try:
        tipo_catasto = request.tipo_catasto or "E"
        request_id = f"soggetto_{tipo_catasto}_{uuid4().hex}"

        soggetto_request = VisuraSoggettoRequest(
            request_id=request_id,
            codice_fiscale=request.codice_fiscale,
            tipo_catasto=tipo_catasto,
            provincia=request.provincia,
        )

        result = await service.add_soggetto_request(soggetto_request, force=force)

        return JSONResponse(
            {
                "request_id": request_id,
                "codice_fiscale": request.codice_fiscale,
                "tipo_catasto": tipo_catasto,
                "provincia": request.provincia or "NAZIONALE",
                "status": "queued",
                "message": f"Ricerca soggetto {request.codice_fiscale} aggiunta alla coda",
                "queue_position": service.request_queue.qsize(),
            }
        )

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Errore nella richiesta soggetto: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def richiedi_visura_persona_giuridica(request: VisuraPersonaGiuridicaInput, service: VisuraService, force: bool = False):
    """Ricerca per persona giuridica (P.IVA o denominazione)."""
    try:
        tipo_catasto = request.tipo_catasto or "E"
        request_id = f"pnf_{tipo_catasto}_{uuid4().hex}"

        pnf_request = VisuraPersonaGiuridicaRequest(
            request_id=request_id,
            identificativo=request.identificativo,
            tipo_catasto=tipo_catasto,
            provincia=request.provincia,
        )

        result = await service.add_persona_giuridica_request(pnf_request, force=force)

        return JSONResponse({
            "request_id": request_id,
            "identificativo": request.identificativo,
            "tipo_catasto": tipo_catasto,
            "provincia": request.provincia or "NAZIONALE",
            "status": "queued",
            "message": f"Ricerca persona giuridica {request.identificativo} aggiunta alla coda",
            "queue_position": service.request_queue.qsize(),
        })

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Errore nella richiesta PNF: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def richiedi_elenco_immobili(request: ElencoImmobiliInput, service: VisuraService, force: bool = False):
    """Elenco immobili per un comune."""
    try:
        tipo_catasto = request.tipo_catasto or "T"
        request_id = f"eimm_{tipo_catasto}_{uuid4().hex}"

        eimm_request = ElencoImmobiliRequest(
            request_id=request_id,
            provincia=request.provincia,
            comune=request.comune,
            tipo_catasto=tipo_catasto,
            foglio=request.foglio,
            sezione=request.sezione,
        )

        result = await service.add_elenco_immobili_request(eimm_request, force=force)

        return JSONResponse({
            "request_id": request_id,
            "provincia": request.provincia,
            "comune": request.comune,
            "tipo_catasto": tipo_catasto,
            "status": "queued",
            "message": f"Elenco immobili per {request.comune} aggiunto alla coda",
            "queue_position": service.request_queue.qsize(),
        })

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Errore nella richiesta elenco immobili: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def execute_workflow(request: WorkflowInput, service: VisuraService):
    """Execute a multi-step workflow preset server-side."""
    from .workflows import run_workflow

    try:
        result = await run_workflow(service, request)
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Errore nel workflow '%s': %s", request.preset, e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def execute_workflow_stream(request: WorkflowInput, service: VisuraService):
    """Execute a workflow with Server-Sent Events for incremental progress."""
    from .workflows import run_workflow_stream

    async def event_generator():
        try:
            async for chunk in run_workflow_stream(service, request):
                yield f"data: {chunk}\n\n"
        except Exception as e:
            import json
            logger.error("Errore nel workflow stream '%s': %s", request.preset, e)
            yield f"data: {json.dumps({'event': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def download_documents(service: VisuraService):
    """Download all available documents from SISTER Richieste page."""
    try:
        documents = await service.download_richieste_documents()
        return JSONResponse({
            "status": "completed",
            "total_documents": len(documents),
            "documents": [{k: v for k, v in d.items() if k != "parsed_data"} for d in documents],
        })
    except Exception as e:
        logger.error("Errore download documenti: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


async def richiedi_ispezione_ipotecaria(request: IspezioneIpotecariaInput, service: VisuraService, force: bool = False):
    """Submit an Ispezione Ipotecaria (paid inspection) request."""
    try:
        tipo_catasto = request.tipo_catasto or "T"
        request_id = f"ipotecaria_{request.tipo_ricerca}_{uuid4().hex}"

        ipotecaria_request = IspezioneIpotecariaRequest(
            request_id=request_id,
            tipo_ricerca=request.tipo_ricerca,
            provincia=request.provincia,
            comune=request.comune,
            tipo_catasto=tipo_catasto,
            codice_fiscale=request.codice_fiscale,
            identificativo=request.identificativo,
            foglio=request.foglio,
            particella=request.particella,
            numero_nota=request.numero_nota,
            anno_nota=request.anno_nota,
            auto_confirm=request.auto_confirm,
        )

        result = await service.add_ispezione_ipotecaria_request(ipotecaria_request, force=force)

        if isinstance(result, SubmitResult) and result.cached and result.response:
            return JSONResponse({
                "request_id": result.request_id,
                "tipo_ricerca": request.tipo_ricerca,
                "status": "cached",
                "data": result.response.data,
            })

        return JSONResponse({
            "request_id": request_id,
            "tipo_ricerca": request.tipo_ricerca,
            "provincia": request.provincia,
            "tipo_catasto": tipo_catasto,
            "auto_confirm": request.auto_confirm,
            "status": "queued",
            "message": f"Ispezione ipotecaria ({request.tipo_ricerca}) aggiunta alla coda",
            "queue_position": service.request_queue.qsize(),
        })

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Errore nella richiesta ispezione ipotecaria: %s", e)
        raise HTTPException(status_code=500, detail="Errore interno del server")


async def richiedi_generic_sister(
    search_type: str,
    provincia: str,
    service: VisuraService,
    comune: Optional[str] = None,
    tipo_catasto: str = "T",
    params: Optional[dict] = None,
    force: bool = False,
):
    """Generic handler for SISTER search types (IND, PART, NOTA, EM, EXPM, OOII, FID, ISP, ISPCART)."""
    try:
        request_id = f"{search_type}_{tipo_catasto}_{uuid4().hex}"

        request = GenericSisterRequest(
            request_id=request_id,
            search_type=search_type,
            provincia=provincia,
            comune=comune,
            tipo_catasto=tipo_catasto,
            params=params or {},
        )

        submit = await service.add_generic_request(request, force=force)

        if isinstance(submit, SubmitResult) and submit.cached and submit.response:
            return JSONResponse({
                "request_id": submit.request_id,
                "search_type": search_type,
                "status": "cached",
                "data": submit.response.data,
            })

        return JSONResponse({
            "request_id": request_id,
            "search_type": search_type,
            "provincia": provincia,
            "tipo_catasto": tipo_catasto,
            "status": "queued",
            "queue_position": service.request_queue.qsize(),
        })

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Errore nella richiesta %s: %s", search_type, e)
        raise HTTPException(status_code=500, detail="Errore interno del server")
