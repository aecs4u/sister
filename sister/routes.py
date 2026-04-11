"""FastAPI route handler functions for sister.

These are plain async functions that accept their dependencies as arguments.
They are registered with FastAPI decorators in main.py.
"""

import logging
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .database import count_responses, find_responses
from .models import (
    AuthenticationError,
    ElencoImmobiliInput,
    ElencoImmobiliRequest,
    GenericSisterRequest,
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
)
from .services import VisuraService

logger = logging.getLogger("sister")


async def richiedi_visura(request: VisuraInput, service: VisuraService):
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
                    foglio=request.foglio,
                    particella=request.particella,
                    subalterno=request.subalterno,
                )
            )
        request_ids = await service.add_requests_batch(visura_requests)

        return JSONResponse(
            {
                "request_ids": request_ids,
                "tipos_catasto": tipos_catasto,
                "status": "queued",
                "message": f"Richieste aggiunte alla coda per {request.comune} F.{request.foglio} P.{request.particella}",
            }
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


async def richiedi_intestati_immobile(request: VisuraIntestatiInput, service: VisuraService):
    """Richiede gli intestati per un immobile specifico."""
    try:
        sezione = None if request.sezione == "_" else request.sezione

        request_id = f"intestati_{request.tipo_catasto}_{uuid4().hex}"

        intestati_request = VisuraIntestatiRequest(
            request_id=request_id,
            tipo_catasto=request.tipo_catasto,
            provincia=request.provincia,
            comune=request.comune,
            foglio=request.foglio,
            particella=request.particella,
            subalterno=request.subalterno,
            sezione=sezione,
        )

        await service.add_intestati_request(intestati_request)

        return JSONResponse(
            {
                "request_id": request_id,
                "tipo_catasto": request.tipo_catasto,
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
            "status": "healthy",
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


async def richiedi_visura_soggetto(request: VisuraSoggettoInput, service: VisuraService):
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

        await service.add_soggetto_request(soggetto_request)

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


async def richiedi_visura_persona_giuridica(request: VisuraPersonaGiuridicaInput, service: VisuraService):
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

        await service.add_persona_giuridica_request(pnf_request)

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


async def richiedi_elenco_immobili(request: ElencoImmobiliInput, service: VisuraService):
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

        await service.add_elenco_immobili_request(eimm_request)

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


async def richiedi_generic_sister(
    search_type: str,
    provincia: str,
    service: VisuraService,
    comune: Optional[str] = None,
    tipo_catasto: str = "T",
    params: Optional[dict] = None,
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

        await service.add_generic_request(request)

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
