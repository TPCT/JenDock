// expect: line 7: unsupported expression
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                echo "now: ${foo()}"
            }
        }
    }
}
