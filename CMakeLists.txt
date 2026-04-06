cmake_minimum_required(VERSION 3.8)
project(xparo)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclpy REQUIRED)
find_package(std_msgs REQUIRED)

add_executable(xparo_c src/xparo_c.cpp)
target_include_directories(xparo_c PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_compile_features(xparo_c PUBLIC c_std_99 cxx_std_17)  # Require C99 and C++17
ament_target_dependencies(
  xparo_c
  "rclcpp"
  "rclpy"
  "std_msgs"
)

install(TARGETS xparo_c
  DESTINATION lib/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  # comment the line when a copyright and license is added to all source files
  set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips cpplint (only works in a git repo)
  # comment the line when this package is in a git repo and when
  # a copyright and license is added to all source files
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

# Install launch files
install(DIRECTORY
  launch
  DESTINATION share/${PROJECT_NAME}/
)

# Install config files
install(DIRECTORY
  config
  DESTINATION share/${PROJECT_NAME}/
)


install(DIRECTORY
  custom_behaviors/
  DESTINATION share/${PROJECT_NAME}/custom_behaviors
)
install(DIRECTORY
  custom_envs/
  DESTINATION share/${PROJECT_NAME}/custom_envs
)
install(DIRECTORY
  properties/
  DESTINATION share/${PROJECT_NAME}/properties
)
install(DIRECTORY
  custom_files/
  DESTINATION share/${PROJECT_NAME}/custom_files
)



# Install Python modules
ament_python_install_package(${PROJECT_NAME})

# Install Python executables
install(PROGRAMS
  ${PROJECT_NAME}/xparo_ros.py
  DESTINATION lib/${PROJECT_NAME}
  RENAME xparo_ros
)


# install(PROGRAMS
#   ${PROJECT_NAME}/xparo_package.py
#   DESTINATION lib/${PROJECT_NAME}/xparo_package
#   RENAME xparo_package.py
# )

install(PROGRAMS
  ${PROJECT_NAME}/database.py
  DESTINATION lib/${PROJECT_NAME}/xparo_package
  RENAME database.py
)





ament_package()
