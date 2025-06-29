"""
FastAPI Application Entry Point
"""
import os
import sys
import asyncio
import threading
import uvicorn
from typing import Optional, Dict
from fastapi import FastAPI, Response, status, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from dotenv import load_dotenv

from entry.config import get_settings
from entry.routers import tts, jobs, voices, streams, debug
from entry.core.models import initialize_models, get_voices

# Global flags to track model loading state
MODELS_LOADED = False
initialized = False
initialization_error = None

# Add thread lock for synchronization
model_init_lock = threading.RLock()

def create_app() -> FastAPI:
    """Create and configure FastAPI application"""
    load_dotenv()
    settings = get_settings()
    
    app = FastAPI(
        title="Kokoro TTS API",
        description="REST API for Kokoro Text-to-Speech Engine",
        version="1.0.0"
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize models on startup
    @app.on_event("startup")
    async def startup_event():
        """Initialize models on startup"""
        global MODELS_LOADED, initialized, initialization_error
        
        logger.info("Initializing models - startup process beginning")
        
        # For Cloud Run, we need to make initialization non-blocking
        # to allow the health check endpoint to respond quickly
        def init_models_thread():
            global MODELS_LOADED, initialized, initialization_error
            try:
                # Use thread lock for safe initialization
                with model_init_lock:
                    # Determine if we need to force online mode for container initialization
                    force_online = False
                    if os.environ.get('CONTAINER_ENV', '').lower() == 'true' or \
                       os.environ.get('K_SERVICE', '').lower() != '':
                        force_online = not os.path.exists(os.path.join(os.getcwd(), 'models', 'Kokoro-82M'))
                        if force_online:
                            logger.info("Container environment detected, forcing online mode for first run")
                    
                    logger.info("Loading models in background thread")
                    # Force a complete model initialization synchronously within the thread
                    initialize_models(force_online=force_online)
                    
                    # Verify models are properly loaded by checking outputs of core functions
                    from entry.core.models import get_models, get_pipelines, get_voices
                    models = get_models()
                    pipelines = get_pipelines()
                    voices = get_voices()
                    
                    # Validate critical components are loaded
                    if not models or not pipelines or not voices:
                        raise RuntimeError(f"Critical components missing after initialization: models={bool(models)}, pipelines={bool(pipelines)}, voices={bool(voices)}")
                        
                    logger.info(f"Models loaded successfully: {len(models)} models, {len(pipelines)} pipelines, {len(voices)} voices")
                    MODELS_LOADED = True
                    initialized = True  # Set initialized flag when models are loaded
                    initialization_error = None  # Clear any previous errors
            except Exception as e:
                logger.error(f"Error loading models: {e}")
                import traceback
                logger.error(f"Initialization error traceback: {traceback.format_exc()}")
                initialization_error = str(e)  # Set error message
                MODELS_LOADED = False
                initialized = False
        
        # Start initialization in a background thread
        thread = threading.Thread(target=init_models_thread)
        thread.daemon = True
        thread.start()

    # Health check endpoints for Cloud Run
    @app.get("/health", tags=["Health"])
    async def health_check():
        """Basic health check that always returns 200 OK (for initial container startup)"""
        if initialization_error:
            return {"status": "error", "error": initialization_error}
        if not initialized:
            return {"status": "initializing"}
        return {"status": "healthy", "voices": list(get_voices())}
    
    @app.get("/ready", tags=["Health"])
    async def readiness_check(response: Response):
        """Readiness check that returns 200 only when models are fully loaded"""
        if initialization_error:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "error", "error": initialization_error}
        if not initialized:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "loading", "message": "Models are still loading"}
        return {"status": "ready", "message": "Models loaded and ready"}
    
    # Include routers
    app.include_router(tts.router, prefix="/tts", tags=["TTS"])
    app.include_router(jobs.router, prefix="/jobs", tags=["Jobs"])
    app.include_router(voices.router, prefix="/voices", tags=["Voices"])
    app.include_router(streams.router, prefix="/streams", tags=["Streams"])
    app.include_router(debug.router, prefix="/debug", tags=["Debug"])

    @app.get("/")
    async def root():
        return {"message": "Kokoro TTS API is running. Visit /docs for API documentation."}

    @app.middleware("http")
    async def check_initialization(request: Request, call_next):
        """Middleware to check if application is initialized"""
        # Allow health checks and readiness checks to pass through
        if request.url.path in ["/health", "/ready", "/docs", "/openapi.json", "/redoc"]:
            return await call_next(request)
        
        # Acquire lock to safely check initialization state
        with model_init_lock:
            if not initialized:
                if initialization_error:
                    # Format and truncate error message to prevent cutoff
                    error_msg = str(initialization_error)
                    if "weights_only" in error_msg:
                        # Special handling for PyTorch 2.6 weights_only errors
                        error_msg = "PyTorch 2.6 compatibility issue with model loading. The model was created with an older PyTorch version. Please restart the application to retry with fallback loading options."
                    
                    # Truncate if too long
                    if len(error_msg) > 200:
                        error_msg = error_msg[:197] + "..."
                        
                    raise HTTPException(
                        status_code=503,
                        detail=f"Service unavailable: {error_msg}"
                    )
                raise HTTPException(
                    status_code=503,
                    detail="Service is still initializing"
                )
            
            # Simple check to ensure models are initialized
            # We don't need to perform detailed validation here since the TTS endpoints
            # will handle specific validation and fallbacks
            from entry.core.models import get_models, get_voices
            models = get_models()
            voices = get_voices()
            
            # Only perform basic checks for non-empty models and voices
            if len(models) == 0 or len(voices) == 0:
                logger.error(f"Critical model components missing at request time: models={len(models)}, voices={len(voices)}")
                raise HTTPException(
                    status_code=503, 
                    detail="Model initialization incomplete - service not ready"
                )
            
            # Log successful validation
            if request.url.path.startswith('/tts'):
                logger.info(f"TTS middleware validation passed: models={len(models)}, voices={len(voices)}")
                
            # For debugging - add detailed info about model components
            if request.url.path.startswith('/debug'):
                from entry.core.models import get_pipelines
                pipelines = get_pipelines()
                logger.info(f"Debug route models: {list(models.keys())}, pipelines: {list(pipelines.keys())}, voices: {list(voices)}")
                
                
        return await call_next(request)

    return app


# Create the application instance
app = create_app()

# Always ensure streams directory exists
streams_dir = os.getenv("STREAMS_DIR", "streams")
os.makedirs(streams_dir, exist_ok=True)

# Only used when running this file directly (development mode)
if __name__ == "__main__":
    # Get configuration from environment variables
    port = int(os.getenv("PORT", 8080))
    host = os.getenv("HOST", "0.0.0.0")
    reload_mode = os.getenv("RELOAD", "False").lower() in ("true", "1", "t")
    
    print(f"Starting development server at {host}:{port} (reload={reload_mode})")
    uvicorn.run("entry.main:app", host=host, port=port, reload=reload_mode)
    
    # NOTE: For production, this block will not be executed.
    # Instead, the container orchestration should:
    # 1. Import the 'app' object directly
    # 2. Set up proper process management
    # 3. Configure via environment variables