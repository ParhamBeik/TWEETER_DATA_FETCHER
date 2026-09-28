# Sourced after cd to the project root. Archive is the deployment default.
# Collection requires an explicit TDF_DEPLOY_MODE=collection and INGESTION_ENABLED=1.
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
SERVICES=(postgres redis web frontend worker_control)
BUILT_IMAGES=(twitter-saas-web twitter-saas-frontend)
case "${TDF_DEPLOY_MODE:-archive}" in
  archive)
    COMPOSE+=(-f docker-compose.archive.yml)
    ;;
  collection)
    SERVICES+=(worker_live worker_historical worker_search beat)
    BUILT_IMAGES+=(twitter-saas-worker_control twitter-saas-worker_live
                   twitter-saas-worker_historical twitter-saas-worker_search twitter-saas-beat)
    ;;
  *) echo "FATAL: TDF_DEPLOY_MODE must be archive or collection" >&2; exit 1 ;;
esac
