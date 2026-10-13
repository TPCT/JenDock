// expect: line 7: must not contain '..'
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                dir('../outside') {
                    echo 'x'
                }
            }
        }
    }
}
