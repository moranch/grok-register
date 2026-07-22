package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	listenAddress  = ":8788"
	statsigEpoch   = int64(1682924400)
	statsigSalt    = "obfiowerehiring"
	statsigMark    = byte(0x03)
	genuineSeedB64 = "t2ODAFY4ozXd0K2Y8MdI2XfxTDiJoakZPuoaKfcQn8VuasZMcKliyhA1pJ+o1oMf"
	genuineHEX     = "3bab9506b851eb851eb840e8f5c28f5c28f80e8f5c28f5c28f806b851eb851eb8400"
)

var genuineSeed = mustDecodeSeed(genuineSeedB64)

type signRequest struct {
	Method      string `json:"method"`
	Path        string `json:"path"`
	Environment struct {
		MetaContent string `json:"metaContent"`
	} `json:"environment"`
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthHandler)
	mux.HandleFunc("POST /sign", signHandler)

	server := &http.Server{
		Addr:              listenAddress,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       30 * time.Second,
	}
	log.Printf("statsig signer listening on %s", listenAddress)
	log.Fatal(server.ListenAndServe())
}

func healthHandler(writer http.ResponseWriter, _ *http.Request) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(http.StatusOK)
	_, _ = writer.Write([]byte(`{"ok":true}`))
}

func signHandler(writer http.ResponseWriter, request *http.Request) {
	request.Body = http.MaxBytesReader(writer, request.Body, 64<<10)
	decoder := json.NewDecoder(request.Body)
	decoder.DisallowUnknownFields()
	var input signRequest
	if err := decoder.Decode(&input); err != nil {
		writeError(writer, http.StatusBadRequest, "invalid JSON payload")
		return
	}
	if err := ensureEOF(decoder); err != nil {
		writeError(writer, http.StatusBadRequest, "invalid trailing JSON data")
		return
	}

	value, err := generateStatsig(input.Path, input.Method, time.Now().Unix())
	if err != nil {
		writeError(writer, http.StatusBadRequest, err.Error())
		return
	}
	writer.Header().Set("Cache-Control", "no-store")
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(map[string]string{"x-statsig-id": value})
}

func ensureEOF(decoder *json.Decoder) error {
	var extra any
	err := decoder.Decode(&extra)
	if errors.Is(err, io.EOF) {
		return nil
	}
	if err == nil {
		return errors.New("unexpected second JSON value")
	}
	return err
}

func generateStatsig(pathname, method string, nowUnix int64) (string, error) {
	method = strings.ToUpper(strings.TrimSpace(method))
	pathname = strings.TrimSpace(pathname)
	if method == "" || strings.IndexFunc(method, func(r rune) bool { return r < 'A' || r > 'Z' }) >= 0 {
		return "", errors.New("invalid method")
	}
	if !strings.HasPrefix(pathname, "/") || strings.ContainsAny(pathname, "?#\x00") {
		return "", errors.New("invalid path")
	}
	if nowUnix < statsigEpoch {
		return "", errors.New("invalid system time")
	}

	number := uint32(nowUnix - statsigEpoch)
	input := method + "!" + pathname + "!" + strconv.FormatUint(uint64(number), 10) + statsigSalt + genuineHEX
	digest := sha256.Sum256([]byte(input))

	var keyBuffer [1]byte
	if _, err := rand.Read(keyBuffer[:]); err != nil {
		return "", fmt.Errorf("random key: %w", err)
	}
	key := keyBuffer[0]
	out := make([]byte, 70)
	out[0] = key
	for index := 0; index < len(genuineSeed); index++ {
		out[index+1] = genuineSeed[index] ^ key
	}
	out[49] = byte(number) ^ key
	out[50] = byte(number>>8) ^ key
	out[51] = byte(number>>16) ^ key
	out[52] = byte(number>>24) ^ key
	for index := 0; index < 16; index++ {
		out[index+53] = digest[index] ^ key
	}
	out[69] = statsigMark ^ key
	return base64.RawStdEncoding.EncodeToString(out), nil
}

func mustDecodeSeed(value string) []byte {
	seed, err := base64.StdEncoding.DecodeString(value)
	if err != nil || len(seed) != 48 {
		panic("invalid embedded Statsig seed")
	}
	return seed
}

func writeError(writer http.ResponseWriter, status int, message string) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": message})
}
