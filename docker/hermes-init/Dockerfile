# Seeds a data directory on a box that has no clone of this repo.
#
# On a developer machine `./hermes/seed.sh` runs from the checkout. A deployed
# box has no checkout — it pulls images and nothing else — so the versioned
# material has to arrive as one of those images. This is that image: it carries
# hermes/ and workflows/, runs the same seed.sh, and exits.
#
# An image rather than material vendored into the public repo, for two reasons.
# hermes/SOUL.md, hermes/skills/ and the profile SOULs are the product, and a
# public git history cannot be unpublished, whereas an image's visibility can be
# changed later. And seed.sh already implements copy-if-absent, render() and
# --update-instructions; a second implementation in a second repo would diverge.
#
# The build context is the repository ROOT. Ignore rules come from the sibling
# Dockerfile.dockerignore rather than the root .dockerignore, which whitelists
# only what the docs image needs — sharing it would either starve this build or
# balloon that one.
FROM alpine:3.21

# bash because seed.sh is bash, not sh. rsync for the workflows tree, where
# --ignore-existing is what keeps a re-run from clobbering agent edits. git for
# track.sh, which commits the curated half of the data directory on every run
# so an upgrade's effect on it is a diff — see docker/hermes-init/track.sh.
RUN apk add --no-cache bash rsync coreutils git

# The versioned material. workflows/ lands on the data disk at runtime; hermes/
# is read from here and copied per seed.sh's own rules.
COPY hermes/ /seed/hermes/
COPY workflows/ /seed/workflows/

# Migrations, and the two scripts that reason about them. The migrations are
# versioned WITH the image on purpose: an upgrade must run the migrations that
# belong to the release it is upgrading TO, and pulling one tag has to be enough
# to get them.
COPY docker/hermes-init/migrations/ /opt/migrations/
COPY docker/hermes-init/marker.sh /usr/local/lib/steward-marker.sh
COPY docker/hermes-init/track.sh /usr/local/lib/steward-track.sh
COPY docker/hermes-init/migrate.sh /usr/local/bin/hermes-migrate

COPY docker/hermes-init/entrypoint.sh /usr/local/bin/hermes-init
RUN chmod +x /usr/local/bin/hermes-init /usr/local/bin/hermes-migrate /seed/hermes/seed.sh

ENTRYPOINT ["/usr/local/bin/hermes-init"]
