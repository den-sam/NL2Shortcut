cmake_minimum_required(VERSION 3.14)
project(nl2shortcut_native LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Build as shared library (DLL)
add_library(nl2shortcut_native SHARED
    src/uia.cpp
    src/inject.cpp
    src/window.cpp
    src/fuzzy_match.cpp
    src/scache.cpp
    src/context_proc.cpp
    src/pattern_cluster.cpp
)

# Windows-specific settings
if(WIN32)
    set_target_properties(nl2shortcut_native PROPERTIES
        PREFIX ""          # no "lib" prefix
        SUFFIX ".dll"
    )
endif()

# Link Windows libraries
target_link_libraries(nl2shortcut_native
    ole32 oleaut32 user32 kernel32
)

# Export all symbols (simplest for ctypes)
target_compile_definitions(nl2shortcut_native PRIVATE _CRT_SECURE_NO_WARNINGS)

# Release build optimization
if(CMAKE_BUILD_TYPE STREQUAL "Release" OR NOT CMAKE_BUILD_TYPE)
    target_compile_options(nl2shortcut_native PRIVATE /O2 /MT)
endif()
