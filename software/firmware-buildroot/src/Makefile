# Optocam Zero native app build.
#
# Driven by the Buildroot package (package/optocamzero/optocamzero.mk), which passes the
# cross CC / CXX / CFLAGS / CXXFLAGS / LDFLAGS via TARGET_CONFIGURE_OPTS and a target
# pkg-config. Also builds standalone with the host toolchain for quick testing:
#
#     make            # cross build when CXX/PKG_CONFIG are set by Buildroot
#     make CXX=g++    # native host build for development
#
# C++17 is applied through OPTO_STD (not CXXFLAGS) so a command-line CXXFLAGS
# override from Buildroot cannot drop the standard that libcamera requires.

CC         ?= gcc
CXX        ?= g++
PKG_CONFIG ?= pkg-config
CFLAGS     ?= -O2
CXXFLAGS   ?= -O2
OPTO_STD   := -std=c++17

APP_CFLAGS := $(shell $(PKG_CONFIG) --cflags libcamera freetype2)
APP_LIBS   := $(shell $(PKG_CONFIG) --libs   libcamera freetype2) -ljpeg -lpthread

PREVIEW_CFLAGS := $(shell $(PKG_CONFIG) --cflags libcamera)
PREVIEW_LIBS   := $(shell $(PKG_CONFIG) --libs   libcamera) -lpthread

all: optocam_app optocam_preview optocam_splash st7789_test

optocam_app: optocam_app.cpp optocam_hud.h optocam_render.h optocam_gif.h
	$(CXX) $(CXXFLAGS) $(OPTO_STD) $(APP_CFLAGS) -o $@ optocam_app.cpp $(APP_LIBS) $(LDFLAGS)

optocam_preview: optocam_preview.cpp
	$(CXX) $(CXXFLAGS) $(OPTO_STD) $(PREVIEW_CFLAGS) -o $@ optocam_preview.cpp $(PREVIEW_LIBS) $(LDFLAGS)

# boot splash (run by S01splash before the app starts) and the panel bring-up
# test — plain C, no libraries
optocam_splash: optocam_splash.c
	$(CC) $(CFLAGS) -o $@ optocam_splash.c $(LDFLAGS)

st7789_test: st7789_test.c
	$(CC) $(CFLAGS) -o $@ st7789_test.c $(LDFLAGS)

clean:
	rm -f optocam_app optocam_preview optocam_splash st7789_test

.PHONY: all clean
