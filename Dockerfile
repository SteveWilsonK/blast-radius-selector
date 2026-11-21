# Same image the commits were labelled in (linux/amd64 on the scorer).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore

# The packages sqlglot's tests import, at the versions in the kit's requirements.lock.
RUN pip install --no-cache-dir \
    duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 python-dateutil==4.0.0 pytz==2026.6 tzdata==2026.6

# sqlglot at the pinned commit, straight from its own repository.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /opt/sqlglot

COPY br /app/br
COPY maps/map.bin maps/refs.bin maps/cond.bin /app/maps/
# The disk is read-only at run time: compile everything now. Then take a fingerprint
# of everything the pinned sqlglot holds once loaded, to compare patched code against.
RUN python -m compileall -q /app/br /opt/sqlglot/sqlglot /opt/sqlglot/tests \
    && cd /app && python -m br.state /opt/sqlglot /app/maps/state.bin \
    && chmod -R a+rX /app /opt/sqlglot

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
CMD ["python", "-m", "br.main"]
