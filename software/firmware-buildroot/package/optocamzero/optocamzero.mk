################################################################################
#
# optocamzero
#
################################################################################

OPTOCAMZERO_VERSION = 1.2.0
OPTOCAMZERO_SITE = $(OPTOCAMZERO_PKGDIR)/src
OPTOCAMZERO_SITE_METHOD = local
OPTOCAMZERO_LICENSE = MIT
OPTOCAMZERO_DEPENDENCIES = libcamera jpeg freetype

define OPTOCAMZERO_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) $(TARGET_CONFIGURE_OPTS) \
		PKG_CONFIG="$(PKG_CONFIG_HOST_BINARY)" \
		-C $(@D)
endef

define OPTOCAMZERO_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/optocam_app $(TARGET_DIR)/usr/bin/optocam_app
	$(INSTALL) -D -m 0755 $(@D)/optocam_preview $(TARGET_DIR)/usr/bin/optocam_preview
	$(INSTALL) -D -m 0755 $(@D)/optocam_splash $(TARGET_DIR)/usr/bin/optocam_splash
	$(INSTALL) -D -m 0755 $(@D)/st7789_test $(TARGET_DIR)/usr/bin/st7789_test
endef

$(eval $(generic-package))
