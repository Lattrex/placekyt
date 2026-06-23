find_package(PkgConfig)

PKG_CHECK_MODULES(PC_GR_KYTTAR gnuradio-kyttar)

FIND_PATH(
    GR_KYTTAR_INCLUDE_DIRS
    NAMES gnuradio/kyttar/api.h
    HINTS $ENV{KYTTAR_DIR}/include
        ${PC_KYTTAR_INCLUDEDIR}
    PATHS ${CMAKE_INSTALL_PREFIX}/include
          /usr/local/include
          /usr/include
)

FIND_LIBRARY(
    GR_KYTTAR_LIBRARIES
    NAMES gnuradio-kyttar
    HINTS $ENV{KYTTAR_DIR}/lib
        ${PC_KYTTAR_LIBDIR}
    PATHS ${CMAKE_INSTALL_PREFIX}/lib
          ${CMAKE_INSTALL_PREFIX}/lib64
          /usr/local/lib
          /usr/local/lib64
          /usr/lib
          /usr/lib64
          )

include("${CMAKE_CURRENT_LIST_DIR}/gnuradio-kyttarTarget.cmake")

INCLUDE(FindPackageHandleStandardArgs)
FIND_PACKAGE_HANDLE_STANDARD_ARGS(GR_KYTTAR DEFAULT_MSG GR_KYTTAR_LIBRARIES GR_KYTTAR_INCLUDE_DIRS)
MARK_AS_ADVANCED(GR_KYTTAR_LIBRARIES GR_KYTTAR_INCLUDE_DIRS)
