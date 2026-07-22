FROM golang:1.24-alpine AS build

WORKDIR /src
COPY go.mod main.go main_test.go ./
RUN go test ./... \
  && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/statsig-signer .

FROM alpine:3.22
RUN addgroup -S signer && adduser -S -G signer signer
COPY --from=build /out/statsig-signer /usr/local/bin/statsig-signer
USER signer
EXPOSE 8788
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1:8788/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/statsig-signer"]
