<samp>🇰🇷 한국어 · [🇺🇸 English](README.en.md)</samp>

# ir-search

> ⚠️ **한국(대한민국) 정부·공공기관 지원사업 전용**입니다. 다른 국가의 지원 프로그램은 다루지 않습니다.

한국 정부·공공기관 **지원사업 전수조사** Claude Code 스킬.

K-Startup·기업마당(bizinfo)·NIPA·KOCCA·SMTECH의 모집중 공고를 크롤링해서, 현재 작업 중인 프로젝트(아이템)의 프로필 — 창업 단계·지역·필요(자금/공간/R&D) — 에 맞는 사업을 골라내고, 상세공고 원문으로 자격요건을 검증한 뒤 3단계로 분류한 보고서를 만들어 줍니다:

- **A그룹 — 지금 즉시 지원 가능**: 현재 신분 그대로 자격 충족 (마감순, 임박 강조)
- **B그룹 — 요건 충족 시 (로드맵)**: 법인 설립·투자유치 등 트리거와 연쇄 경로 명시
- **C그룹 — 변형하면 가능**: 아이템을 다른 분야 언어로 재서술하는 프레이밍 각도 제안

키워드 검색이 아니라 전수 검토를 하는 이유: "AI 스타트업"이 지원할 수 있는 콘텐츠 제작지원·예술×기술 입주·사회서비스 창업지원 같은 사업은 키워드로 잡히지 않기 때문입니다.

## 산출물 예시 (발췌)

실행하면 `~/Documents/지원사업조사_<대상>_<날짜>/`에 보고서 md + 원시 jsonl + 상세공고 원문이 저장됩니다. 보고서는 이런 식입니다:

```markdown
# 지원사업 전수조사 — ○○ (AI 음성 SaaS, 예비창업자, 충남)
조사일 2026-07-11 · K-Startup 262건 + 기업마당 300건 전수 검토 → 후보 31건 상세 검증

## A그룹 — 지금 즉시 지원 가능 (마감순)

1. **2026 청년창업사관학교 추가모집** — 중소벤처기업진흥공단
   - 지원: 사업화 자금 최대 1억 원 + 입주공간 + 멘토링
   - 자격: 예비창업자 포함 ✓ · 만 39세 이하 ✓ · 전국 접수 ✓
   - 마감: 2026-07-18 16:00 (D-7) ⚠️ 임박
   - https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?schM=view&pbancSn=1784xx

## B그룹 — 요건 충족 시 열림 (로드맵)

- **프리팁스(Pre-TIPS)**: 트리거 = 비수도권 법인 설립.
  연쇄 경로: 경진대회 상금·시드 → 충남 법인 설립 → 프리팁스 → TIPS
  - https://www.k-startup.go.kr/...&pbancSn=1779xx

## C그룹 — 변형(프레이밍)하면 가능

- **콘텐츠 제작지원 (KOCCA)**: "AI 음성 기술"이 아니라 "오디오 콘텐츠
  제작 파이프라인"으로 재서술하면 대상. 리스크: 결과물이 콘텐츠여야 함
  - https://www.kocca.kr/...

## 부재 확인
- 예비창업패키지: 현재 모집중 아님 (통상 2월 공고 — 알림 설정 권장)

## 우선순위 액션
- ~7/18: A-1 청창사 신청 (16:00 마감 주의)
- ~7/25: C-1 콘텐츠 프레이밍 초안 작성 후 문의처 유선확인
```

모든 공고에 원문 URL이 붙고, 공고에 없는 정보는 추정하지 않고 '불명'으로 표기합니다.

## 커버 소스

| 소스 | 내용 | 크롤러 |
|---|---|---|
| [K-Startup](https://www.k-startup.go.kr) | 창업지원 통합 (기본) | `kstartup_crawl.py` |
| [기업마당](https://www.bizinfo.go.kr) | 전 부처·지자체 중소기업 지원 (최대 커버리지) | `sources_crawl.py` |
| [NIPA](https://www.nipa.kr) | AI/ICT 사업 | `sources_crawl.py` |
| [KOCCA](https://www.kocca.kr) | 콘텐츠 지원 | `sources_crawl.py` |
| [SMTECH](https://www.smtech.go.kr) | 중기부 R&D | `sources_crawl.py` |

그 외 소스(NIA·IITP·IRIS·지역기관 등)는 `references/sources.md`의 레지스트리 참조.

## 설치

```bash
git clone https://github.com/djfksjd/ir-search.git ~/.claude/skills/ir-search
pip install 'curl_cffi>=0.15'   # 권장 (TLS 지문 차단 회피)
```

## 사용

Claude Code에서 프로젝트 폴더를 연 상태로:

```
우리 아이템에 맞는 지원사업 전수조사 해줘
```

또는 `/ir-search`. Claude가 폴더에서 프로젝트 정보를 읽고, 비는 항목(창업 단계·지역·필요한 것)만 물어본 뒤 조사를 시작합니다.

**반복 사용을 전제로 설계되어 있습니다:**
- 프로필은 프로젝트 폴더의 `ir-search-profile.md`에 저장 — 다음 조사부터는 다시 묻지 않고 "바뀐 것 있나요?" 한 번만 확인
- 재조사 시 직전 결과와 자동 비교(diff)해서 **신규 공고 / 마감 변경 / 종료된 기회**만 증분 보고 — 250건+를 매번 다시 읽지 않습니다

크롤러는 단독으로도 쓸 수 있습니다:

```bash
python3 scripts/kstartup_crawl.py list -o all.jsonl            # K-Startup 모집중 전수
python3 scripts/kstartup_crawl.py detail 178481 -o details/    # K-Startup 상세공고
python3 scripts/sources_crawl.py list bizinfo -o biz.jsonl     # 기업마당
python3 scripts/sources_crawl.py list all -o sources.jsonl     # 4개 소스 일괄
python3 scripts/sources_crawl.py detail <URL> -o details/      # 소스 무관 상세공고
```

## 구성

```
ir-search/
├── SKILL.md                    # 워크플로 (프로필 → 전수수집 → 전수검토 → 상세검증 → 3분류 보고)
├── scripts/
│   ├── kstartup_crawl.py       # K-Startup 크롤러
│   ├── sources_crawl.py        # 기업마당·NIPA·KOCCA·SMTECH 크롤러
│   └── diff_surveys.py         # 재조사 증분 비교 (신규/마감변경/종료)
└── references/sources.md       # 소스 레지스트리 (검증된 접근법 + 보조 소스)
```

## 주의

- 공고 내용(마감일·자격요건·금액)은 수시로 바뀝니다. **신청 전 반드시 접수기관에 확인**하세요. 이 스킬의 산출물은 조사 시점의 공고 텍스트 기준입니다.
- 공개 공고 페이지만 접근하며 요청 간 지연을 둡니다. 대상 사이트의 이용약관을 존중해 주세요.

## License

MIT
