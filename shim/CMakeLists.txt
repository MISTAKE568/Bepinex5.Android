cmake_minimum_required(VERSION 3.18)
project(be5hook C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# Allow a build-time directory name when the game uses a nested Managed layout.
if(DEFINED GAME_DATA_DIR)
  add_definitions(-DGAME_DATA_DIR="${GAME_DATA_DIR}")
endif()

add_library(be5hook SHARED mono_bepinex_hook.c asset_copy.c)
target_link_libraries(be5hook PRIVATE log dl android)
