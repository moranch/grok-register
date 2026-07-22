package main

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestGenerateStatsigShapeAndRequestBinding(t *testing.T) {
	first, err := generateStatsig("/rest/rate-limits", "post", 1784707000)
	if err != nil {
		t.Fatal(err)
	}
	second, err := generateStatsig("/rest/app-chat/conversations/new", "POST", 1784707000)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := base64.RawStdEncoding.DecodeString(first)
	if err != nil || len(decoded) != 70 {
		t.Fatalf("decoded statsig length=%d err=%v", len(decoded), err)
	}
	if first == second {
		t.Fatal("different request paths produced identical signatures")
	}
}

func TestSignHandler(t *testing.T) {
	body := `{"method":"POST","path":"/rest/rate-limits","environment":{"metaContent":"ignored"}}`
	request := httptest.NewRequest(http.MethodPost, "/sign", strings.NewReader(body))
	response := httptest.NewRecorder()
	signHandler(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	var payload map[string]string
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload["x-statsig-id"]) != 94 {
		t.Fatalf("signature length=%d", len(payload["x-statsig-id"]))
	}
}

func TestSignHandlerRejectsAbsoluteURL(t *testing.T) {
	body := `{"method":"POST","path":"https://example.test/rest/rate-limits"}`
	request := httptest.NewRequest(http.MethodPost, "/sign", strings.NewReader(body))
	response := httptest.NewRecorder()
	signHandler(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status=%d", response.Code)
	}
}
